import { useState, useMemo } from 'react'
import {
  useNotificationDefinitions,
  useFireNotification,
  useDeleteNotification,
  usePauseNotification,
  useResumeNotification,
} from '../../api/notifications'
import type { NotificationDefinition } from '../../api/notifications'
import { useAgents } from '../../api/agents'
import { useAdminUsers } from '../../api/runs'
import { formatIntervalDescription } from '../../lib/format'
import TransferredFrom from '../../components/TransferredFrom'
import { errorText } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import { Select } from '../../components/ui/Field'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, BUTTON_DANGER_QUIET, CARD, PAGE_TITLE } from '../../components/ui/classes'

const SEVERITY_BADGE: Record<string, string> = {
  info: BADGE_TONE.info,
  success: BADGE_TONE.success,
  warning: BADGE_TONE.warning,
  danger: BADGE_TONE.danger,
}


const SCOPE_LABEL: Record<string, string> = {
  user: 'User',
  agent: 'All users',
  global: 'Global',
}

function formatSchedule(n: NotificationDefinition): string {
  if (n.notification_type === 'recurring') {
    if (n.interval_seconds) return formatIntervalDescription(n.interval_seconds)
    if (n.schedule) return n.schedule
  }
  if (n.run_at) {
    const d = new Date(n.run_at)
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
  }
  return 'Immediate'
}

export default function NotificationsPage() {
  const dialogs = useDialogs()
  const [agentFilter, setAgentFilter] = useState('')
  const [userFilter, setUserFilter] = useState('')
  // Admin audit page: full-audit view (every user's notifications, audit=true);
  // agent/user are filtered client-side below.
  const { data: allNotifications, isLoading } = useNotificationDefinitions(undefined, { audit: true })
  const { data: agents } = useAgents({ all: true })
  const { data: users } = useAdminUsers()
  const fireNow = useFireNotification()
  const deleteNotif = useDeleteNotification()
  const pauseNotif = usePauseNotification()
  const resumeNotif = useResumeNotification()
  const [feedback, setFeedback] = useState<{ msg: string; ok: boolean } | null>(null)

  const agentNames = useMemo(() => (agents ?? []).map(a => a.name).sort(), [agents])
  const notifications = useMemo(() => {
    if (!allNotifications) return []
    let filtered = allNotifications
    if (agentFilter) {
      filtered = filtered.filter(n =>
        (n.scope === 'agent' && n.target === agentFilter) || n.scope === 'global'
      )
    }
    if (userFilter) {
      filtered = filtered.filter(n => n.created_by === userFilter)
    }
    return filtered
  }, [allNotifications, agentFilter, userFilter])

  const showFeedback = (msg: string, ok: boolean) => {
    setFeedback({ msg, ok })
    setTimeout(() => setFeedback(null), 3000)
  }

  const handleFire = async (n: NotificationDefinition) => {
    try {
      await fireNow.mutateAsync(n.id)
      showFeedback(`Fired: ${n.title}`, true)
    } catch (e) {
      showFeedback(errorText(e) || 'The notification was not fired. Try again.', false)
    }
  }

  const handleDelete = async (n: NotificationDefinition) => {
    if (!(await dialogs.confirm({ title: `Delete the notification "${n.title}"?`, body: 'This cannot be undone.', confirmLabel: 'Delete', tone: 'danger' }))) return
    try {
      await deleteNotif.mutateAsync(n.id)
      showFeedback(`Deleted: ${n.title}`, true)
    } catch (e) {
      showFeedback(errorText(e) || 'The notification was not deleted. Try again.', false)
    }
  }

  const handlePause = async (n: NotificationDefinition) => {
    try {
      await pauseNotif.mutateAsync(n.id)
      showFeedback(`Paused: ${n.title}`, true)
    } catch (e) {
      showFeedback(errorText(e) || 'The notification was not paused. Try again.', false)
    }
  }

  const handleResume = async (n: NotificationDefinition) => {
    try {
      await resumeNotif.mutateAsync(n.id)
      showFeedback(`Resumed: ${n.title}`, true)
    } catch (e) {
      showFeedback(errorText(e) || 'The notification was not resumed. Try again.', false)
    }
  }

  const items = notifications

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end md:justify-between">
        <h1 className={PAGE_TITLE}>Notifications</h1>
        {feedback && (
          <span className={`${BADGE_BASE} ${BADGE_TONE[feedback.ok ? 'success' : 'danger']}`}>
            {feedback.msg}
          </span>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <Select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="sm:w-auto"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </Select>
        <Select
          value={userFilter}
          onChange={(e) => setUserFilter(e.target.value)}
          className="sm:w-auto"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </Select>
        <span className="text-xs text-secondary ml-auto">{items.length} notifications</span>
      </div>

      {isLoading ? (
        <p className="text-sm text-secondary py-4">Loading...</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-secondary py-4">No notifications found.</p>
      ) : (
        <>
          {/* Desktop table */}
          <div className={`${CARD} hidden md:block overflow-hidden`}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
                    <th className="px-4 py-3">Title</th>
                    <th className="px-4 py-3">Agent</th>
                    <th className="px-4 py-3">Severity</th>
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3">Schedule</th>
                    <th className="px-4 py-3">Scope</th>
                    <th className="px-4 py-3">Source</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(n => (
                    <tr key={n.id} className="border-b border-subtle hover:bg-surface-hover">
                      <td className="px-4 py-3">
                        <div className="font-medium text-primary">{n.title}</div>
                        <TransferredFrom row={n} className="block" />
                        <div className="text-xs text-tertiary mt-0.5 line-clamp-1">{n.body}</div>
                      </td>
                      <td className="px-4 py-3 text-secondary">{n.scope === 'agent' ? n.target : '\u2014'}</td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${SEVERITY_BADGE[n.severity] || SEVERITY_BADGE.info}`}>
                          {n.severity}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-secondary">
                        {n.notification_type === 'recurring' ? 'Recurring' : 'One-time'}
                      </td>
                      <td className="px-4 py-3 text-secondary font-mono text-xs">
                        {formatSchedule(n)}
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-xs text-secondary">
                          {n.scope === 'user' ? n.target_name || 'User' : SCOPE_LABEL[n.scope] || n.scope}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
                          {n.source}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${BADGE_TONE[n.enabled ? 'success' : 'warning']}`}>
                          {n.enabled ? 'Active' : 'Paused'}
                        </span>
                      </td>
                      {/* nowrap: the table wrapper scrolls horizontally, so the
                          action buttons stay on one line instead of stacking. */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        <div className="flex items-center gap-1.5">
                          {n.can_fire && (
                            <Button variant="secondary" size="xs"
                              onClick={() => handleFire(n)}
                              disabled={fireNow.isPending}>
                              Fire
                            </Button>
                          )}
                          {n.can_pause && (
                            <Button variant="secondary" size="xs"
                              onClick={() => handlePause(n)}
                              disabled={pauseNotif.isPending}>
                              Pause
                            </Button>
                          )}
                          {n.can_resume && (
                            <Button variant="secondary" size="xs"
                              onClick={() => handleResume(n)}
                              disabled={resumeNotif.isPending}>
                              Resume
                            </Button>
                          )}
                          {n.can_delete && (
                            <button
                              onClick={() => handleDelete(n)}
                              disabled={deleteNotif.isPending}
                              className={BUTTON_DANGER_QUIET}
                            >
                              Delete
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Mobile cards */}
          <div className="md:hidden space-y-3">
            {items.map(n => (
              <div key={n.id} className={`${CARD} p-4`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="text-sm font-medium text-primary truncate">{n.title}</h3>
                    <TransferredFrom row={n} className="block" />
                    <p className="text-xs text-secondary">{n.scope === 'agent' ? n.target : ''}</p>
                    <p className="text-xs text-tertiary mt-0.5 line-clamp-2">{n.body}</p>
                  </div>
                  <span className={`${BADGE_BASE} ${SEVERITY_BADGE[n.severity] || SEVERITY_BADGE.info} shrink-0`}>
                    {n.severity}
                  </span>
                </div>
                <div className="mt-3 space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-tertiary">Type</span>
                    <span className="text-secondary">{n.notification_type === 'recurring' ? 'Recurring' : 'One-time'}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-tertiary">Schedule</span>
                    <span className="text-secondary font-mono">{formatSchedule(n)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-tertiary">Scope</span>
                    <span className="text-secondary">{n.scope === 'user' ? n.target_name || 'User' : SCOPE_LABEL[n.scope] || n.scope}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-tertiary">Status</span>
                    <span className={`${BADGE_BASE} ${BADGE_TONE[n.enabled ? 'success' : 'warning']}`}>
                      {n.enabled ? 'Active' : 'Paused'}
                    </span>
                  </div>
                </div>
                {(n.can_fire || n.can_pause || n.can_resume || n.can_delete) && (
                  <div className="flex flex-wrap items-center gap-2 mt-3 pt-3 border-t border-subtle">
                    {n.can_fire && (
                      <Button variant="secondary" size="xs" onClick={() => handleFire(n)} disabled={fireNow.isPending}>
                        Fire Now
                      </Button>
                    )}
                    {n.can_pause && (
                      <Button variant="secondary" size="xs" onClick={() => handlePause(n)} disabled={pauseNotif.isPending}>
                        Pause
                      </Button>
                    )}
                    {n.can_resume && (
                      <Button variant="secondary" size="xs" onClick={() => handleResume(n)} disabled={resumeNotif.isPending}>
                        Resume
                      </Button>
                    )}
                    {n.can_delete && (
                      <button onClick={() => handleDelete(n)} disabled={deleteNotif.isPending}
                        className={BUTTON_DANGER_QUIET}>
                        Delete
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
