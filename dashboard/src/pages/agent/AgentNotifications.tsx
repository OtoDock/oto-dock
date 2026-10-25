import { useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  useNotificationDefinitions,
  useFireNotification,
  useDeleteNotification,
  usePauseNotification,
  useResumeNotification,
} from '../../api/notifications'
import type { NotificationDefinition } from '../../api/notifications'
import { formatIntervalDescription } from '../../lib/format'
import { ScopeFilterSelect, type ScopeFilterValue } from '../../components/ScopeFilterSelect'
import TransferredFrom from '../../components/TransferredFrom'
import { errorText } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, CARD, BUTTON_DANGER_QUIET } from '../../components/ui/classes'

const SEVERITY_BADGE: Record<string, string> = {
  info: BADGE_TONE.info,
  success: BADGE_TONE.success,
  warning: BADGE_TONE.warning,
  danger: BADGE_TONE.danger,
}

const SCOPE_LABEL: Record<string, string> = {
  user: 'You',
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

function formatCreatedAt(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export default function AgentNotifications() {
  const dialogs = useDialogs()
  const { name } = useParams<{ name: string }>()
  const { data: notifications, isLoading } = useNotificationDefinitions(name)
  const fireNow = useFireNotification()
  const deleteNotif = useDeleteNotification()
  const pauseNotif = usePauseNotification()
  const resumeNotif = useResumeNotification()
  const [feedback, setFeedback] = useState<{ msg: string; ok: boolean } | null>(null)
  const [scopeFilter, setScopeFilter] = useState<ScopeFilterValue>('all')

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

  if (isLoading) {
    return <div className="text-sm text-tertiary py-8 text-center">Loading notifications...</div>
  }

  const items = (notifications || []).filter((n) => scopeFilter === 'all' || n.scope === scopeFilter)

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold font-display text-primary">Notifications</h2>
        {feedback && (
          <span className={`${BADGE_BASE} ${feedback.ok ? BADGE_TONE.success : BADGE_TONE.danger}`}>
            {feedback.msg}
          </span>
        )}
      </div>

      {/* Scope filter */}
      <div className="flex items-center gap-2 flex-wrap mb-4">
        <ScopeFilterSelect value={scopeFilter} onChange={setScopeFilter} />
        <span className="text-xs text-secondary ml-auto">{items.length} total</span>
      </div>

      {items.length === 0 ? (
        <p className="text-sm text-tertiary py-4">
          No notifications configured for this agent. Agents can create notifications via the notifications MCP tool.
        </p>
      ) : (
        <>
          {/* Desktop table */}
          <div className={`hidden md:block ${CARD} overflow-hidden`}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-surface-sunken text-secondary text-xs uppercase tracking-wider">
                    <th className="text-left px-4 py-2.5 font-medium">Title</th>
                    <th className="text-left px-4 py-2.5 font-medium">Severity</th>
                    <th className="text-left px-4 py-2.5 font-medium">Type</th>
                    <th className="text-left px-4 py-2.5 font-medium">Schedule</th>
                    <th className="text-left px-4 py-2.5 font-medium">Scope</th>
                    <th className="text-left px-4 py-2.5 font-medium">Source</th>
                    <th className="text-left px-4 py-2.5 font-medium">Status</th>
                    <th className="text-left px-4 py-2.5 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(n => (
                    <tr key={n.id} className="border-t border-subtle hover:bg-surface-hover transition-colors">
                      <td className="px-4 py-3">
                        <div className="font-medium text-primary">{n.title}</div>
                        <TransferredFrom row={n} className="block" />
                        <div className="text-xs text-tertiary mt-0.5 line-clamp-1">{n.body}</div>
                      </td>
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
                          {n.scope === 'user' ? n.target_name || 'You' : SCOPE_LABEL[n.scope] || n.scope}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
                          {n.source}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${n.enabled ? BADGE_TONE.success : BADGE_TONE.neutral}`}>
                          {n.enabled ? 'Active' : 'Paused'}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          {n.can_fire && (
                            <Button
                              size="xs"
                              onClick={() => handleFire(n)}
                              disabled={fireNow.isPending}
                            >
                              Fire
                            </Button>
                          )}
                          {n.can_pause && (
                            <Button
                              size="xs"
                              onClick={() => handlePause(n)}
                              disabled={pauseNotif.isPending}
                            >
                              Pause
                            </Button>
                          )}
                          {n.can_resume && (
                            <Button
                              size="xs"
                              onClick={() => handleResume(n)}
                              disabled={resumeNotif.isPending}
                            >
                              Resume
                            </Button>
                          )}
                          {n.can_delete && (
                            <button
                              type="button" className={BUTTON_DANGER_QUIET}
                              onClick={() => handleDelete(n)}
                              disabled={deleteNotif.isPending}
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
                    <p className="text-xs text-tertiary mt-0.5 line-clamp-2">{n.body}</p>
                  </div>
                  <span className={`shrink-0 ${BADGE_BASE} ${SEVERITY_BADGE[n.severity] || SEVERITY_BADGE.info}`}>
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
                    <span className="text-secondary">{SCOPE_LABEL[n.scope] || n.scope}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-tertiary">Source</span>
                    <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>{n.source}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-tertiary">Status</span>
                    <span className={`${BADGE_BASE} ${n.enabled ? BADGE_TONE.success : BADGE_TONE.neutral}`}>
                      {n.enabled ? 'Active' : 'Paused'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-tertiary">Created</span>
                    <span className="text-secondary">{formatCreatedAt(n.created_at)}</span>
                  </div>
                </div>

                {(n.can_fire || n.can_pause || n.can_resume || n.can_delete) && (
                  <div className="flex flex-wrap items-center gap-2 mt-3 pt-3 border-t border-subtle">
                    {n.can_fire && (
                      <Button
                        size="xs"
                        onClick={() => handleFire(n)}
                        disabled={fireNow.isPending}
                      >
                        Fire Now
                      </Button>
                    )}
                    {n.can_pause && (
                      <Button
                        size="xs"
                        onClick={() => handlePause(n)}
                        disabled={pauseNotif.isPending}
                      >
                        Pause
                      </Button>
                    )}
                    {n.can_resume && (
                      <Button
                        size="xs"
                        onClick={() => handleResume(n)}
                        disabled={resumeNotif.isPending}
                      >
                        Resume
                      </Button>
                    )}
                    {n.can_delete && (
                      <button
                        type="button" className={BUTTON_DANGER_QUIET}
                        onClick={() => handleDelete(n)}
                        disabled={deleteNotif.isPending}
                      >
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
