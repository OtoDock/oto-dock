import { useState, useMemo, useEffect } from 'react'
import { Trigger, useCreateTrigger, useEditTrigger, useFireTrigger } from '../../api/triggers'
import { useTasks } from '../../api/tasks'
import { useAgentInfo } from '../../api/agents'
import { availableScopes, modeOfAgent } from '../../lib/visibility'


// =====================================================================
// Modals
// =====================================================================

export function CreateTriggerModal({ agent, onClose, canCreateAgentScope }: { agent: string; onClose: () => void; canCreateAgentScope: boolean }) {
  // Seed the form's initial scope from the agent's mode. Single-scope modes
  // (Personal only → user; Shared only → agent) force their only scope and
  // hide the toggle. Collaborative modes seed from `default_scope` so managers
  // on operational agents don't have to flip every time. Viewers still start
  // at 'user'. Falls back to 'user' until AgentInfo loads, then syncs once.
  const { data: info } = useAgentInfo(agent)
  const scopes = info ? availableScopes(modeOfAgent(info)) : null
  const forcedScope = scopes && scopes.length === 1 ? scopes[0] : null
  const [scope, setScope] = useState<'user' | 'agent'>('user')
  const [scopeTouched, setScopeTouched] = useState(false)
  useEffect(() => {
    if (!info) return
    const sc = availableScopes(modeOfAgent(info))
    if (sc.length === 1) {
      setScope(sc[0]) // single-scope mode — always force it
      return
    }
    if (scopeTouched) return
    if (canCreateAgentScope && info.default_scope === 'agent') setScope('agent')
  }, [info, canCreateAgentScope, scopeTouched])
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [taskId, setTaskId] = useState('')
  const [notifyEnabled, setNotifyEnabled] = useState(true)
  const [notifyTitle, setNotifyTitle] = useState('')
  const [notifyBody, setNotifyBody] = useState('')
  const [notifySeverity, setNotifySeverity] = useState('info')
  const [debounce, setDebounce] = useState(0)
  // Optional vendor-subscription linkage. Empty = generic
  // webhook URL (otok_-authed). Non-empty = trigger fires from a
  // vendor subscription that matches event_filter.
  const [sourceType, setSourceType] = useState<'generic' | 'vendor'>('generic')
  const [subscriptionId, setSubscriptionId] = useState('')
  const [eventFilterText, setEventFilterText] = useState('{}')

  const createM = useCreateTrigger()
  const { data: tasks = [] } = useTasks(agent)
  // Trigger-only tasks for this scope
  const eligibleTasks = useMemo(
    () => tasks.filter((t: any) => t.task_type === 'trigger' && t.scope === scope),
    [tasks, scope],
  )

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      alert('Name required')
      return
    }
    if (!taskId && !notifyEnabled) {
      alert('Pick a task or enable notification (at least one action required).')
      return
    }
    if (notifyEnabled && (!notifyTitle.trim() || !notifyBody.trim())) {
      alert('Notify title and body required when notify is enabled.')
      return
    }
    let parsedFilter: Record<string, unknown> | undefined
    if (sourceType === 'vendor') {
      if (!subscriptionId.trim()) {
        alert('Vendor source requires a subscription_id. Create the subscription from Connected Accounts first.')
        return
      }
      try {
        parsedFilter = eventFilterText.trim() ? JSON.parse(eventFilterText) : {}
      } catch (e) {
        alert(`event_filter must be valid JSON: ${(e as Error).message}`)
        return
      }
    }
    try {
      await createM.mutateAsync({
        name: name.trim(),
        scope,
        agent,
        slug: slug.trim() || undefined,
        task_id: taskId || undefined,
        notify: notifyEnabled
          ? {
              enabled: true,
              severity: notifySeverity as any,
              title: notifyTitle,
              body: notifyBody,
            }
          : undefined,
        debounce_seconds: debounce,
        subscription_id: sourceType === 'vendor' ? subscriptionId.trim() : undefined,
        event_filter: sourceType === 'vendor' ? parsedFilter : undefined,
      })
      onClose()
    } catch (e: any) {
      alert(`Create failed: ${e?.message ?? e}`)
    }
  }

  return (
    <Modal onClose={onClose} title="Create trigger">
      <form onSubmit={onSubmit} className="space-y-3">
        {!forcedScope && (
          <Field label="Scope">
            <select
              value={scope}
              onChange={(e) => {
                setScope(e.target.value as 'user' | 'agent')
                setScopeTouched(true)
              }}
              className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
            >
              <option value="user">User (personal automation)</option>
              {canCreateAgentScope && <option value="agent">Agent (manager-managed)</option>}
            </select>
          </Field>
        )}
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. GitHub PR opened"
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
          />
        </Field>
        <Field label="Slug (optional)" hint="URL-safe id; auto-derived from name if blank">
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="e.g. github-pr-opened"
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm font-mono"
          />
        </Field>
        <Field label="Source" hint="Generic = your code POSTs to a webhook URL with an OtoDock API key. Vendor = a connected Slack/GitHub/etc. subscription fires this trigger on matching events.">
          <select
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value as 'generic' | 'vendor')}
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
          >
            <option value="generic">Generic webhook URL</option>
            <option value="vendor">Vendor subscription</option>
          </select>
        </Field>
        {sourceType === 'vendor' && (
          <>
            <Field label="Subscription ID" hint="An active subscription's ID from one of your connected accounts (User Settings → Integrations → Connected Accounts → expand the account → Active subscriptions).">
              <input
                value={subscriptionId}
                onChange={(e) => setSubscriptionId(e.target.value)}
                placeholder="e.g. 8f3a-...-uuid"
                className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm font-mono"
              />
            </Field>
            <Field label="Event filter (JSON, empty = match all)" hint='Match a specific event by its catalog key, e.g. {"event_type": "pull_request"}. Leave empty ({}) to fire on every event from this subscription.'>
              <textarea
                value={eventFilterText}
                onChange={(e) => setEventFilterText(e.target.value)}
                rows={3}
                className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm font-mono"
              />
            </Field>
          </>
        )}
        <Field label="Task to run (optional)">
          <select
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
          >
            <option value="">— None —</option>
            {eligibleTasks.map((t: any) => (
              <option key={t.id} value={t.id}>
                {t.name} ({t.id.slice(0, 8)})
              </option>
            ))}
          </select>
          {eligibleTasks.length === 0 && (
            <p className="text-xs text-amber-600 mt-1">
              No trigger-only tasks for this scope. Ask an agent to "create a trigger-only
              task" via schedules-mcp first.
            </p>
          )}
        </Field>
        <div className="space-y-3 border-t border-p-border-light pt-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={notifyEnabled}
              onChange={(e) => setNotifyEnabled(e.target.checked)}
            />
            <span>Send notification on fire</span>
          </label>
          {notifyEnabled && (
            <>
              <Field label="Severity">
                <select
                  value={notifySeverity}
                  onChange={(e) => setNotifySeverity(e.target.value)}
                  className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
                >
                  <option value="info">info</option>
                  <option value="success">success</option>
                  <option value="warning">warning</option>
                  <option value="danger">danger (alarm)</option>
                </select>
              </Field>
              <Field label="Notification title" hint="Use {{placeholder}} to substitute webhook payload values">
                <input
                  value={notifyTitle}
                  onChange={(e) => setNotifyTitle(e.target.value)}
                  placeholder="e.g. PR merged: {{title}}"
                  className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
                />
              </Field>
              <Field label="Notification body">
                <textarea
                  value={notifyBody}
                  onChange={(e) => setNotifyBody(e.target.value)}
                  rows={2}
                  placeholder="e.g. {{author}} merged #{{number}}"
                  className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
                />
              </Field>
            </>
          )}
        </div>
        <Field label="Debounce (seconds)" hint="Minimum seconds between consecutive fires (0 = no debounce)">
          <input
            type="number"
            min={0}
            value={debounce}
            onChange={(e) => setDebounce(parseInt(e.target.value) || 0)}
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
          />
        </Field>
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={createM.isPending}
            className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover disabled:opacity-50"
          >
            {createM.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </form>
    </Modal>
  )
}


export function EditTriggerModal({ trigger, onClose }: { trigger: Trigger; onClose: () => void }) {
  const [name, setName] = useState(trigger.name)
  const [notifyEnabled, setNotifyEnabled] = useState(trigger.notify_enabled)
  const [notifyTitle, setNotifyTitle] = useState(trigger.notify_title ?? '')
  const [notifyBody, setNotifyBody] = useState(trigger.notify_body ?? '')
  const [notifySeverity, setNotifySeverity] = useState(trigger.notify_severity)
  const [debounce, setDebounce] = useState(trigger.debounce_seconds)
  const editM = useEditTrigger()

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await editM.mutateAsync({
        id: trigger.id,
        fields: {
          name,
          notify_enabled: notifyEnabled,
          notify_severity: notifySeverity,
          notify_title: notifyTitle || null,
          notify_body: notifyBody || null,
          debounce_seconds: debounce,
        },
      })
      onClose()
    } catch (e: any) {
      alert(`Edit failed: ${e?.message ?? e}`)
    }
  }

  return (
    <Modal onClose={onClose} title="Edit trigger">
      <form onSubmit={onSubmit} className="space-y-3">
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={notifyEnabled}
            onChange={(e) => setNotifyEnabled(e.target.checked)}
          />
          <span>Send notification on fire</span>
        </label>
        {notifyEnabled && (
          <>
            <Field label="Severity">
              <select
                value={notifySeverity}
                onChange={(e) => setNotifySeverity(e.target.value)}
                className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
              >
                <option value="info">info</option>
                <option value="success">success</option>
                <option value="warning">warning</option>
                <option value="danger">danger (alarm)</option>
              </select>
            </Field>
            <Field label="Notification title" hint="Use {{placeholder}} to substitute webhook payload values">
              <input
                value={notifyTitle}
                onChange={(e) => setNotifyTitle(e.target.value)}
                placeholder="e.g. PR merged: {{title}}"
                className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
              />
            </Field>
            <Field label="Notification body">
              <textarea
                value={notifyBody}
                onChange={(e) => setNotifyBody(e.target.value)}
                rows={2}
                placeholder="e.g. {{author}} merged #{{number}}"
                className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
              />
            </Field>
          </>
        )}
        <Field label="Debounce (seconds)">
          <input
            type="number"
            min={0}
            value={debounce}
            onChange={(e) => setDebounce(parseInt(e.target.value) || 0)}
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
          />
        </Field>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm">
            Cancel
          </button>
          <button
            type="submit"
            disabled={editM.isPending}
            className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover disabled:opacity-50"
          >
            {editM.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </form>
    </Modal>
  )
}


export function FireTestModal({ trigger, onClose }: { trigger: Trigger; onClose: () => void }) {
  const [bodyJson, setBodyJson] = useState('{\n  "name": "test"\n}')
  const [result, setResult] = useState<any>(null)
  const fireM = useFireTrigger()

  const onFire = async () => {
    let body: Record<string, unknown> = {}
    try {
      body = bodyJson.trim() ? JSON.parse(bodyJson) : {}
    } catch {
      alert('Invalid JSON')
      return
    }
    try {
      const r = await fireM.mutateAsync({ id: trigger.id, body })
      setResult(r)
    } catch (e: any) {
      setResult({ error: e?.message ?? String(e) })
    }
  }

  return (
    <Modal onClose={onClose} title={`Test fire: ${trigger.name}`}>
      <div className="space-y-3">
        <Field label="Sample webhook body (JSON)" hint="Used for {{placeholder}} substitution">
          <textarea
            value={bodyJson}
            onChange={(e) => setBodyJson(e.target.value)}
            rows={5}
            className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm font-mono"
          />
        </Field>
        {result && (
          <pre className="text-xs bg-p-bg rounded-sm p-2 overflow-auto max-h-60">
            {JSON.stringify(result, null, 2)}
          </pre>
        )}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm">
            Close
          </button>
          <button
            onClick={onFire}
            disabled={fireM.isPending}
            className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover disabled:opacity-50"
          >
            {fireM.isPending ? 'Firing…' : 'Fire'}
          </button>
        </div>
      </div>
    </Modal>
  )
}


// =====================================================================
// Reusable bits
// =====================================================================

export function Modal({ onClose, title, children }: { onClose: () => void; title: string; children: React.ReactNode }) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light max-w-lg w-full max-h-[90vh] overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-p-border-light flex items-center justify-between">
          <h2 className="font-semibold text-p-text">{title}</h2>
          <button onClick={onClose} className="text-p-text-secondary hover:text-p-text text-xl leading-none">&times;</button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-p-text mb-1">{label}</label>
      {children}
      {hint && <p className="text-xs text-p-text-secondary mt-1">{hint}</p>}
    </div>
  )
}
