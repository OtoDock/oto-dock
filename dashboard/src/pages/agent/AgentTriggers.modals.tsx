import { useState, useMemo } from 'react'
import { Trigger, useCreateTrigger, useEditTrigger, useFireTrigger, NOTIFY_SEVERITIES, type NotifySeverity } from '../../api/triggers'
import { useTasks } from '../../api/tasks'
import { useApps } from '../../api/apps'
import { useAgentInfo } from '../../api/agents'
import { useSubscriptions } from '../../api/subscriptions'
import { useAuth } from '../../contexts/AuthContext'
import { availableScopes, modeOfAgent } from '../../lib/visibility'
import { appKind } from '../../lib/kinds/app'
import { TASK_KIND } from '../../lib/kinds/task'
import { errorText } from '../../lib/http/apiJson'
import Dialog, { type DialogDismiss } from '../../components/ui/Dialog'
import Button from '../../components/ui/Button'
import { Input, Label, Select } from '../../components/ui/Field'
import InfoTip from '../../components/ui/InfoTip'
import { DIALOG_PANEL, FIELD, FIELD_BORDER, FIELD_REGULAR } from '../../components/ui/classes'

/** The "paste an id" choice of the subscription picker. */
const PASTE_ID = '__paste__'


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
  const [pickedScope, setPickedScope] = useState<'user' | 'agent'>('user')
  const [scopeTouched, setScopeTouched] = useState(false)
  const scope: 'user' | 'agent' = forcedScope
    ?? (!scopeTouched && canCreateAgentScope && info?.default_scope === 'agent' ? 'agent' : pickedScope)
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [taskId, setTaskId] = useState('')
  const [notifyEnabled, setNotifyEnabled] = useState(true)
  const [notifyTitle, setNotifyTitle] = useState('')
  const [notifyBody, setNotifyBody] = useState('')
  const [notifySeverity, setNotifySeverity] = useState<NotifySeverity>('info')
  const [debounce, setDebounce] = useState(0)
  // Optional vendor-subscription linkage. Empty = generic
  // webhook URL (otok_-authed). Non-empty = trigger fires from a
  // vendor subscription that matches event_filter.
  const [sourceType, setSourceType] = useState<'generic' | 'vendor'>('generic')
  const [subscriptionChoice, setSubscriptionChoice] = useState('')
  const [pastedSubscriptionId, setPastedSubscriptionId] = useState('')
  const subscriptionId = subscriptionChoice === PASTE_ID ? pastedSubscriptionId : subscriptionChoice
  const [eventFilterText, setEventFilterText] = useState('{}')
  // A trigger links only to a subscription of its own scope: personal rows
  // for a user trigger, the agent's rows for an agent trigger. Offering just
  // those makes the cross-scope refusal unreachable from here.
  const { user } = useAuth()
  const subsQuery = useSubscriptions(
    scope === 'agent' ? { scope: 'service', agent } : { scope: 'user' },
  )
  const linkableSubs = useMemo(
    () => (subsQuery.data ?? []).filter((s) =>
      scope === 'agent' ? s.agent === agent : s.owner === user?.sub),
    [subsQuery.data, scope, agent, user?.sub],
  )

  const createM = useCreateTrigger()
  const { data: tasks = [] } = useTasks(agent)
  // Trigger-only tasks for this scope
  const eligibleTasks = useMemo(
    () => tasks.filter((t) => t.task_type === TASK_KIND.TRIGGER && t.scope === scope),
    [tasks, scope],
  )
  // Apps with a server that declared on_trigger handlers, in this scope
  // (APPS.md "Handlers"): the fire wakes the handler instead of a task.
  const [appTarget, setAppTarget] = useState('')   // "<slug>|<handler>"
  // A subscription, an app handler and a task each belong to one scope:
  // a scope change drops the picks made for the other, which the select no
  // longer shows but the create would still send (and the server refuse).
  const [picksFor, setPicksFor] = useState(scope)
  if (picksFor !== scope) {
    setPicksFor(scope)
    setSubscriptionChoice('')
    setPastedSubscriptionId('')
    setAppTarget('')
    setTaskId('')
  }
  const { data: apps = [] } = useApps(agent)
  const eligibleApps = useMemo(
    () => apps.filter((a) => appKind(a).mayServe && (a.handlers?.on_trigger?.length ?? 0) > 0
      && (scope === 'agent' ? a.scope === 'shared' : a.scope === 'personal')),
    [apps, scope],
  )

  const [formError, setFormError] = useState('')

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError('')
    if (!name.trim()) {
      setFormError('Give the trigger a name.')
      return
    }
    if (!taskId && !appTarget && !notifyEnabled) {
      setFormError('Pick a task or an app to wake, or turn on the notification. A trigger needs at least one action.')
      return
    }
    if (notifyEnabled && (!notifyTitle.trim() || !notifyBody.trim())) {
      setFormError('The notification needs a title and a body.')
      return
    }
    let parsedFilter: Record<string, unknown> | undefined
    if (sourceType === 'vendor') {
      if (!subscriptionId.trim()) {
        setFormError(scope === 'agent'
          ? "A vendor source needs one of this agent's subscriptions. Add one in the agent's MCPs. On the MCP, click Subscribe to events for this agent."
          : 'A vendor source needs one of your subscriptions. Add one in User Settings, Integrations, Connected Accounts.')
        return
      }
      try {
        parsedFilter = eventFilterText.trim() ? JSON.parse(eventFilterText) : {}
      } catch (e) {
        setFormError(`The event filter is not valid JSON: ${(e as Error).message}`)
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
        app_slug: appTarget ? appTarget.split('|')[0] : undefined,
        handler: appTarget ? appTarget.split('|')[1] : undefined,
        notify: notifyEnabled
          ? {
              enabled: true,
              severity: notifySeverity,
              title: notifyTitle,
              body: notifyBody,
            }
          : undefined,
        debounce_seconds: debounce,
        subscription_id: sourceType === 'vendor' ? subscriptionId.trim() : undefined,
        event_filter: sourceType === 'vendor' ? parsedFilter : undefined,
      })
      onClose()
    } catch (e) {
      setFormError(errorText(e) || 'The trigger was not created.')
    }
  }

  return (
    <Modal onClose={onClose} title="Create trigger" busy={createM.isPending}>
      <form onSubmit={onSubmit} className="space-y-3">
        {!forcedScope && (
          <Field label="Scope">
            <Select
              value={scope}
              onChange={(e) => {
                setPickedScope(e.target.value as 'user' | 'agent')
                setScopeTouched(true)
              }}
            >
              <option value="user">User: a personal automation</option>
              {canCreateAgentScope && <option value="agent">Agent: managed by the agent's managers</option>}
            </Select>
          </Field>
        )}
        <Field label="Name">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. GitHub PR opened"
          />
        </Field>
        <Field label="ID, optional" info="Used in the webhook URL." hint="Made from the name if left blank.">
          <Input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="e.g. github-pr-opened"
            className="font-mono"
          />
        </Field>
        <Field label="Source" info="Generic: your code sends a POST to the webhook URL with an OtoDock API key. Vendor: a connected subscription, like Slack or GitHub, fires this trigger on matching events.">
          <Select
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value as 'generic' | 'vendor')}
          >
            <option value="generic">Generic webhook URL</option>
            <option value="vendor">Vendor subscription</option>
          </Select>
        </Field>
        {sourceType === 'vendor' && (
          <>
            <Field
              label="Subscription"
              info={scope === 'agent'
                ? "One of this agent's subscriptions. Add one in the agent's MCPs. On the MCP, click Subscribe to events for this agent."
                : 'One of your personal subscriptions. Add one in User Settings, Integrations, Connected Accounts. Expand the account and click Subscribe to events.'}
            >
              <Select
                value={subscriptionChoice}
                onChange={(e) => setSubscriptionChoice(e.target.value)}
                data-testid="trigger-subscription"
              >
                <option value="">Choose…</option>
                {linkableSubs.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.provider_id}/{s.vendor_target} · {s.selected_events.join(', ') || '—'}
                  </option>
                ))}
                <option value={PASTE_ID}>Paste a subscription id…</option>
              </Select>
              {subscriptionChoice === PASTE_ID && (
                <Input
                  value={pastedSubscriptionId}
                  onChange={(e) => setPastedSubscriptionId(e.target.value)}
                  placeholder="e.g. 8f3a-...-uuid"
                  className="mt-1 font-mono"
                />
              )}
              {!subsQuery.isLoading && linkableSubs.length === 0 && (
                <p className="text-xs text-status-warning-fg mt-1">
                  {scope === 'agent'
                    ? "No agent subscriptions yet. Add one in the agent's MCPs. On the MCP, click Subscribe to events for this agent."
                    : 'No personal subscriptions yet. Add one in User Settings, Integrations, Connected Accounts. Expand the account and click Subscribe to events.'}
                </p>
              )}
            </Field>
            <Field label="Event filter in JSON, empty matches all" hint='Match a specific event by its key, e.g. {"event_type": "pull_request"}. Leave empty ({}) to fire on every event from this subscription.'>
              <textarea
                value={eventFilterText}
                onChange={(e) => setEventFilterText(e.target.value)}
                rows={3}
                className={`${FIELD} ${FIELD_BORDER} ${FIELD_REGULAR} font-mono`}
              />
            </Field>
          </>
        )}
        <Field label="App to wake, optional" info="An app with a server can take the trigger: its app.json lists handlers.on_trigger. A fire then wakes the app instead of running a task. Approve the app first.">
          <Select
            value={appTarget}
            onChange={(e) => { setAppTarget(e.target.value); if (e.target.value) setTaskId('') }}
            data-testid="trigger-app-handler"
          >
            <option value="">None</option>
            {eligibleApps.flatMap((a) => (a.handlers?.on_trigger ?? []).map((h) => (
              <option key={`${a.slug}|${h}`} value={`${a.slug}|${h}`} disabled={!a.actions_approved}>
                {a.title || a.slug} → {h}{a.actions_approved ? '' : ', approve the app first'}
              </option>
            )))}
          </Select>
        </Field>
        <Field label="Task to run, optional">
          <Select
            value={taskId}
            onChange={(e) => { setTaskId(e.target.value); if (e.target.value) setAppTarget('') }}
          >
            <option value="">None</option>
            {eligibleTasks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} ({t.id.slice(0, 8)})
              </option>
            ))}
          </Select>
          {eligibleTasks.length === 0 && (
            <p className="text-xs text-status-warning-fg mt-1">
              No trigger-only tasks for this scope. Ask an agent to "create a trigger-only
              task" via schedules-mcp first.
            </p>
          )}
        </Field>
        <div className="space-y-3 border-t border-subtle pt-3">
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
                <Select
                  value={notifySeverity}
                  onChange={(e) => setNotifySeverity(NOTIFY_SEVERITIES.find((s) => s === e.target.value) ?? 'info')}
                >
                  <option value="info">Info</option>
                  <option value="success">Success</option>
                  <option value="warning">Warning</option>
                  <option value="danger">Danger, with an alarm</option>
                </Select>
              </Field>
              <Field label="Notification title" hint="Use {{placeholder}} to fill in values from the webhook body">
                <Input
                  value={notifyTitle}
                  onChange={(e) => setNotifyTitle(e.target.value)}
                  placeholder="e.g. PR merged: {{title}}"
                />
              </Field>
              <Field label="Notification body">
                <textarea
                  value={notifyBody}
                  onChange={(e) => setNotifyBody(e.target.value)}
                  rows={2}
                  placeholder="e.g. {{author}} merged #{{number}}"
                  className={`${FIELD} ${FIELD_BORDER} ${FIELD_REGULAR}`}
                />
              </Field>
            </>
          )}
        </div>
        <Field label="Debounce in seconds" info="The fewest seconds between two fires." hint="0 turns it off.">
          <Input
            type="number"
            min={0}
            value={debounce}
            onChange={(e) => setDebounce(parseInt(e.target.value) || 0)}
          />
        </Field>
        <FormError text={formError} />
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={createM.isPending}>
            {createM.isPending ? 'Creating…' : 'Create'}
          </Button>
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
  // The vendor source can be bound after creation — an app's trigger is
  // usually made before the agent's subscription exists, and a trigger
  // with no subscription never hears a vendor event. Same scope rule as
  // the create modal: the agent's rows for an agent trigger, personal rows
  // for a user trigger.
  const [subscriptionId, setSubscriptionId] = useState(trigger.subscription_id ?? '')
  const { user } = useAuth()
  const subsQuery = useSubscriptions(
    trigger.scope === 'agent' ? { scope: 'service', agent: trigger.agent } : { scope: 'user' },
  )
  const linkableSubs = useMemo(
    () => (subsQuery.data ?? []).filter((s) =>
      trigger.scope === 'agent' ? s.agent === trigger.agent : s.owner === user?.sub),
    [subsQuery.data, trigger.scope, trigger.agent, user?.sub],
  )
  const editM = useEditTrigger()
  const [formError, setFormError] = useState('')

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError('')
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
          ...(subscriptionId !== (trigger.subscription_id ?? '') ? { subscription_id: subscriptionId } : {}),
        },
      })
      onClose()
    } catch (e) {
      setFormError(errorText(e) || 'The trigger was not saved.')
    }
  }

  return (
    <Modal onClose={onClose} title="Edit trigger" busy={editM.isPending}>
      <form onSubmit={onSubmit} className="space-y-3">
        <Field label="Name">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field
          label="Vendor subscription"
          info={trigger.scope === 'agent'
            ? "The agent's subscription this trigger fires from. Add one in the agent's MCPs. On the MCP, click Subscribe to events for this agent."
            : 'The personal subscription this trigger fires from. Add one in User Settings, Integrations, Connected Accounts. Expand the account and click Subscribe to events.'}
          hint="None means only the generic webhook URL fires it."
        >
          <Select
            value={subscriptionId}
            onChange={(e) => setSubscriptionId(e.target.value)}
            data-testid="edit-trigger-subscription"
          >
            <option value="">None</option>
            {linkableSubs.map((s) => (
              <option key={s.id} value={s.id}>
                {s.provider_id}/{s.vendor_target} · {s.selected_events.join(', ') || '—'}
              </option>
            ))}
            {subscriptionId && !linkableSubs.some((s) => s.id === subscriptionId) && (
              <option value={subscriptionId}>{subscriptionId}, current</option>
            )}
          </Select>
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
              <Select
                value={notifySeverity}
                onChange={(e) => setNotifySeverity(e.target.value)}
              >
                <option value="info">Info</option>
                <option value="success">Success</option>
                <option value="warning">Warning</option>
                <option value="danger">Danger, with an alarm</option>
              </Select>
            </Field>
            <Field label="Notification title" hint="Use {{placeholder}} to fill in values from the webhook body">
              <Input
                value={notifyTitle}
                onChange={(e) => setNotifyTitle(e.target.value)}
                placeholder="e.g. PR merged: {{title}}"
              />
            </Field>
            <Field label="Notification body">
              <textarea
                value={notifyBody}
                onChange={(e) => setNotifyBody(e.target.value)}
                rows={2}
                placeholder="e.g. {{author}} merged #{{number}}"
                className={`${FIELD} ${FIELD_BORDER} ${FIELD_REGULAR}`}
              />
            </Field>
          </>
        )}
        <Field label="Debounce in seconds">
          <Input
            type="number"
            min={0}
            value={debounce}
            onChange={(e) => setDebounce(parseInt(e.target.value) || 0)}
          />
        </Field>
        <FormError text={formError} />
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={editM.isPending}>
            {editM.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}


export function FireTestModal({ trigger, onClose }: { trigger: Trigger; onClose: () => void }) {
  const [bodyJson, setBodyJson] = useState('{\n  "name": "test"\n}')
  const [result, setResult] = useState<unknown>(null)
  const [jsonError, setJsonError] = useState('')
  const fireM = useFireTrigger()

  const onFire = async () => {
    let body: Record<string, unknown>
    try {
      body = bodyJson.trim() ? JSON.parse(bodyJson) : {}
    } catch {
      setJsonError('The sample body is not valid JSON.')
      return
    }
    setJsonError('')
    try {
      const r = await fireM.mutateAsync({ id: trigger.id, body })
      setResult(r)
    } catch (e) {
      setResult({ error: errorText(e) || String(e) })
    }
  }

  return (
    <Modal onClose={onClose} title={`Test fire: ${trigger.name}`} busy={fireM.isPending}>
      <div className="space-y-3">
        <Field label="Sample webhook body in JSON" info="Used for {{placeholder}} substitution">
          <textarea
            value={bodyJson}
            onChange={(e) => { setBodyJson(e.target.value); setJsonError('') }}
            rows={5}
            aria-invalid={!!jsonError || undefined}
            className={`${FIELD} ${FIELD_BORDER} ${FIELD_REGULAR} font-mono`}
          />
        </Field>
        <FormError text={jsonError} />
        {result ? (
          <pre className="text-xs bg-surface-sunken rounded-sm p-2 overflow-auto max-h-60">
            {JSON.stringify(result, null, 2)}
          </pre>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>
            Close
          </Button>
          <Button variant="primary" onClick={onFire} disabled={fireM.isPending}>
            {fireM.isPending ? 'Firing…' : 'Fire'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}


// =====================================================================
// Reusable bits
// =====================================================================

/** The trigger and key dialogs' shell: a title with Close, the body padded.
 *  `dismissible="escape"` for a one-time secret a stray click must not lose. */
export function Modal({ onClose, title, busy = false, dismissible = 'any', children }: {
  onClose: () => void
  title: string
  busy?: boolean
  dismissible?: DialogDismiss
  children: React.ReactNode
}) {
  return (
    <Dialog
      onClose={onClose}
      title={title}
      busy={busy}
      dismissible={dismissible}
      panelClassName={`${DIALOG_PANEL} max-w-lg`}
    >
      <div className="p-4">{children}</div>
    </Dialog>
  )
}

/** A form's refusal, read out when it appears. */
export function FormError({ text }: { text: string }) {
  if (!text) return null
  return <p role="alert" className="text-xs text-status-danger-fg">{text}</p>
}

export function Field({ label, hint, info, children }: { label: string; hint?: string; info?: string; children: React.ReactNode }) {
  return (
    <div>
      {info ? (
        <div className="mb-1">
          <label className="text-xs font-medium text-primary">{label}</label>
          <InfoTip label={label}>{info}</InfoTip>
        </div>
      ) : (
        <Label>{label}</Label>
      )}
      {children}
      {hint && <p className="text-xs text-secondary mt-1">{hint}</p>}
    </div>
  )
}
