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

const SEVERITY_BADGE: Record<string, string> = {
  info: 'bg-brand/10 text-brand',
  success: 'bg-p-accent-teal/10 text-p-accent-teal',
  warning: 'bg-p-accent-yellow/10 text-[#b8860b]',
  danger: 'bg-p-accent-red/10 text-p-accent-red',
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
    } catch (e: any) {
      showFeedback(`Failed: ${e.message}`, false)
    }
  }

  const handleDelete = async (n: NotificationDefinition) => {
    if (!window.confirm(`Delete notification "${n.title}"? This cannot be undone.`)) return
    try {
      await deleteNotif.mutateAsync(n.id)
      showFeedback(`Deleted: ${n.title}`, true)
    } catch (e: any) {
      showFeedback(`Failed: ${e.message}`, false)
    }
  }

  const handlePause = async (n: NotificationDefinition) => {
    try {
      await pauseNotif.mutateAsync(n.id)
      showFeedback(`Paused: ${n.title}`, true)
    } catch (e: any) {
      showFeedback(`Failed: ${e.message}`, false)
    }
  }

  const handleResume = async (n: NotificationDefinition) => {
    try {
      await resumeNotif.mutateAsync(n.id)
      showFeedback(`Resumed: ${n.title}`, true)
    } catch (e: any) {
      showFeedback(`Failed: ${e.message}`, false)
    }
  }

  const items = notifications

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-p-text">Notifications</h1>
        {feedback && (
          <span className={`text-xs px-2.5 py-1 rounded-full ${feedback.ok ? 'bg-p-accent-teal/10 text-p-accent-teal' : 'bg-p-accent-red/10 text-p-accent-red'}`}>
            {feedback.msg}
          </span>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <select
          value={userFilter}
          onChange={(e) => setUserFilter(e.target.value)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </select>
        <span className="text-xs text-p-text-secondary ml-auto">{items.length} notifications</span>
      </div>

      {isLoading ? (
        <p className="text-sm text-p-text-secondary py-4">Loading...</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-p-text-secondary py-4">No notifications found.</p>
      ) : (
        <>
          {/* Desktop table */}
          <div className="hidden md:block bg-white dark:bg-p-surface rounded-xl border border-p-border-light overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-p-bg text-left text-xs text-p-text-secondary uppercase tracking-wide border-b border-p-border-light">
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
                    <tr key={n.id} className="border-b border-p-border-light hover:bg-p-surface-hover">
                      <td className="px-4 py-3">
                        <div className="font-medium text-p-text">{n.title}</div>
                        <div className="text-xs text-p-text-light mt-0.5 line-clamp-1">{n.body}</div>
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary">{n.scope === 'agent' ? n.target : '\u2014'}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${SEVERITY_BADGE[n.severity] || SEVERITY_BADGE.info}`}>
                          {n.severity}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary">
                        {n.notification_type === 'recurring' ? 'Recurring' : 'One-time'}
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary font-mono text-xs">
                        {formatSchedule(n)}
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-xs text-p-text-secondary">
                          {n.scope === 'user' ? (n as any).target_name || 'User' : SCOPE_LABEL[n.scope] || n.scope}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium bg-brand/10 text-brand">
                          {n.source}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${
                          n.enabled ? 'bg-green-100 text-green-700 dark:bg-green-900/20 dark:text-green-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
                        }`}>
                          {n.enabled ? 'Active' : 'Paused'}
                        </span>
                      </td>
                      {/* nowrap: the table wrapper scrolls horizontally, so the
                          action buttons stay on one line instead of stacking. */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        <div className="flex items-center gap-1.5">
                          {n.can_fire && (
                            <button
                              onClick={() => handleFire(n)}
                              disabled={fireNow.isPending}
                              className="px-2 py-1 text-xs rounded-sm bg-brand/10 text-brand hover:bg-brand/20 transition-colors disabled:opacity-50"
                            >
                              Fire
                            </button>
                          )}
                          {n.can_pause && (
                            <button
                              onClick={() => handlePause(n)}
                              disabled={pauseNotif.isPending}
                              className="px-2 py-1 text-xs rounded-sm bg-amber-100 text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:hover:bg-amber-900/50 transition-colors disabled:opacity-50"
                            >
                              Pause
                            </button>
                          )}
                          {n.can_resume && (
                            <button
                              onClick={() => handleResume(n)}
                              disabled={resumeNotif.isPending}
                              className="px-2 py-1 text-xs rounded-sm bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50 transition-colors disabled:opacity-50"
                            >
                              Resume
                            </button>
                          )}
                          {n.can_delete && (
                            <button
                              onClick={() => handleDelete(n)}
                              disabled={deleteNotif.isPending}
                              className="px-2 py-1 text-xs rounded-sm bg-p-accent-red/10 text-p-accent-red hover:bg-p-accent-red/20 transition-colors disabled:opacity-50"
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
              <div key={n.id} className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="text-sm font-medium text-p-text truncate">{n.title}</h3>
                    <p className="text-xs text-p-text-secondary">{n.scope === 'agent' ? n.target : ''}</p>
                    <p className="text-xs text-p-text-light mt-0.5 line-clamp-2">{n.body}</p>
                  </div>
                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${SEVERITY_BADGE[n.severity] || SEVERITY_BADGE.info}`}>
                    {n.severity}
                  </span>
                </div>
                <div className="mt-3 space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-p-text-light">Type</span>
                    <span className="text-p-text-secondary">{n.notification_type === 'recurring' ? 'Recurring' : 'One-time'}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-p-text-light">Schedule</span>
                    <span className="text-p-text-secondary font-mono">{formatSchedule(n)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-p-text-light">Scope</span>
                    <span className="text-p-text-secondary">{n.scope === 'user' ? (n as any).target_name || 'User' : SCOPE_LABEL[n.scope] || n.scope}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-p-text-light">Status</span>
                    <span className={`inline-flex items-center px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${
                      n.enabled ? 'bg-green-100 text-green-700 dark:bg-green-900/20 dark:text-green-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
                    }`}>
                      {n.enabled ? 'Active' : 'Paused'}
                    </span>
                  </div>
                </div>
                {(n.can_fire || n.can_pause || n.can_resume || n.can_delete) && (
                  <div className="flex flex-wrap items-center gap-2 mt-3 pt-3 border-t border-p-border-light">
                    {n.can_fire && (
                      <button onClick={() => handleFire(n)} disabled={fireNow.isPending}
                        className="px-3 py-1.5 text-xs rounded-lg bg-brand/10 text-brand hover:bg-brand/20 transition-colors disabled:opacity-50">
                        Fire Now
                      </button>
                    )}
                    {n.can_pause && (
                      <button onClick={() => handlePause(n)} disabled={pauseNotif.isPending}
                        className="px-3 py-1.5 text-xs rounded-lg bg-amber-100 text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:hover:bg-amber-900/50 transition-colors disabled:opacity-50">
                        Pause
                      </button>
                    )}
                    {n.can_resume && (
                      <button onClick={() => handleResume(n)} disabled={resumeNotif.isPending}
                        className="px-3 py-1.5 text-xs rounded-lg bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50 transition-colors disabled:opacity-50">
                        Resume
                      </button>
                    )}
                    {n.can_delete && (
                      <button onClick={() => handleDelete(n)} disabled={deleteNotif.isPending}
                        className="px-3 py-1.5 text-xs rounded-lg bg-p-accent-red/10 text-p-accent-red hover:bg-p-accent-red/20 transition-colors disabled:opacity-50">
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
