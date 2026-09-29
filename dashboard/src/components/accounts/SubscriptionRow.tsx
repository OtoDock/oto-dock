/**
 * One webhook subscription row, shared by the Connected Accounts panel and
 * the agent's MCPs tab: status + scope pills, the vendor target, the events,
 * the manual-mode webhook URL / secret, and a Delete that walks the
 * linked-triggers refusal (409 → confirm → force).
 */

import { useState } from 'react'
import {
  fetchSigningSecret,
  linkedTriggersOf,
  SubscriptionRequestError,
  useDeleteSubscription,
  type WebhookSubscription,
} from '../../api/subscriptions'

/** Plain words for a refused subscription request. Structured refusals
 * (missing scopes, a duplicate) say what to do; anything else is the
 * server's message. */
export function describeSubscriptionError(
  err: unknown,
  opts: { scope: 'user' | 'service'; accountLabel: string },
): string {
  if (err instanceof SubscriptionRequestError && typeof err.detail !== 'string') {
    const d = err.detail
    if (d.error === 'missing_scopes') {
      const scopes = Array.isArray(d.required) ? d.required.join(', ') : ''
      return opts.scope === 'service'
        ? `The bound account ${opts.accountLabel} is missing the scopes ${scopes}. Its owner must reconnect it with the matching permission ticked.`
        : `Your account is missing the scopes ${scopes}. Reconnect it with the matching permission ticked and try again.`
    }
    if (d.error === 'exists') {
      return 'A subscription for this target already exists here.'
    }
  }
  return (err as Error).message
}

/**
 * Delete with the two confirmations: the plain one, then, when the server
 * answers that triggers still fire from the row, a second one before the
 * forced delete leaves those triggers without a source.
 */
export function useDeleteWithConfirm() {
  const del = useDeleteSubscription()
  const [error, setError] = useState<string | null>(null)

  const remove = async (row: WebhookSubscription) => {
    setError(null)
    const consequence =
      row.delivery_mode === 'relay'
        ? 'OtoDock stops forwarding its events to this install.'
        : 'This unregisters the webhook at the vendor.'
    if (!confirm(`Delete subscription for ${row.vendor_target}? ${consequence}`)) {
      return
    }
    // The row goes even when the vendor keeps the registration
    // (orphan-tolerant); that outcome is the one thing worth saying.
    const report = (res: { vendor_detached?: boolean }) => {
      if (res.vendor_detached === false) {
        setError(
          `The subscription is deleted but the webhook at the vendor could not be removed. Delete it by hand in the vendor's settings for ${row.vendor_target}.`,
        )
      }
    }
    try {
      report(await del.mutateAsync({ id: row.id }))
      return
    } catch (err) {
      const linked = linkedTriggersOf(err)
      if (linked.length === 0) {
        setError((err as Error).message)
        return
      }
      const names = linked.map((t) => t.name).join(', ')
      if (
        !confirm(
          `${linked.length} trigger${linked.length === 1 ? '' : 's'} still fire${linked.length === 1 ? 's' : ''} from this subscription: ${names}. Delete it anyway? The trigger${linked.length === 1 ? ' stays' : 's stay'} but no longer receive${linked.length === 1 ? 's' : ''} events.`,
        )
      ) {
        return
      }
    }
    try {
      report(await del.mutateAsync({ id: row.id, force: true }))
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return { remove, isPending: del.isPending, error, setError }
}

export function SubscriptionRow({
  sub,
  webhookBase,
  registrationMode,
  perSubscriptionSecret,
  agentLabel,
  onDelete,
  isPending,
}: {
  sub: WebhookSubscription
  webhookBase: string
  registrationMode: 'relay' | 'auto' | 'manual'
  perSubscriptionSecret: boolean
  /** Display name of the agent a service row belongs to. */
  agentLabel?: string
  onDelete: () => void
  isPending: boolean
}) {
  const [copied, setCopied] = useState<string | null>(null)
  const events = sub.selected_events.join(', ')
  const isRelay = sub.delivery_mode === 'relay'
  // Manual vendor-mode rows need the URL pasted into the vendor console.
  const showWebhookUrl = !isRelay && registrationMode === 'manual'
  const webhookUrl = webhookBase
    ? `${webhookBase}/v1/webhooks/${sub.provider_id}/${sub.id}`
    : ''

  const copy = async (label: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(label)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      /* clipboard unavailable — the URL is still selectable */
    }
  }

  return (
    <li className="flex items-center justify-between gap-2 text-xs" data-testid="subscription-row">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusPill status={sub.status} />
          <ScopePill sub={sub} agentLabel={agentLabel} />
          <span className="font-mono truncate">{sub.vendor_target}</span>
          {sub.target_kind && (
            <span className="inline-block px-1.5 rounded-sm text-[10px] uppercase font-semibold bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200">
              {sub.target_kind}
            </span>
          )}
          {isRelay && (
            <span className="inline-block px-1.5 rounded-sm text-[10px] uppercase font-semibold bg-purple-100 text-purple-800">
              via OtoDock
            </span>
          )}
        </div>
        <div className="text-p-text-light truncate" title={events}>
          {events || '—'}
          {sub.event_count > 0 && (
            <span className="ml-1">
              · {sub.event_count} fired
            </span>
          )}
          {sub.scope === 'service' && sub.created_by_name && (
            <span className="ml-1">· by {sub.created_by_name}</span>
          )}
        </div>
        {showWebhookUrl && (
          webhookUrl ? (
            <div className="flex items-center gap-1 mt-0.5 min-w-0">
              <span
                className="font-mono text-[10px] text-p-text-light truncate"
                title={webhookUrl}
              >
                {webhookUrl}
              </span>
              <button
                type="button"
                onClick={() => copy('url', webhookUrl)}
                className="text-brand hover:underline shrink-0"
              >
                {copied === 'url' ? 'Copied' : 'Copy URL'}
              </button>
              {perSubscriptionSecret && (
                <button
                  type="button"
                  onClick={async () => {
                    try {
                      const secret = await fetchSigningSecret(sub.id)
                      if (!secret) {
                        // Token-capture vendors (Notion): the secret arrives
                        // with the vendor's setup POST — not received yet.
                        setCopied('pending')
                        setTimeout(() => setCopied(null), 2500)
                        return
                      }
                      await copy('secret', secret)
                    } catch {
                      /* manage-gated or platform-wide secret */
                    }
                  }}
                  className="text-brand hover:underline shrink-0"
                >
                  {copied === 'secret'
                    ? 'Copied'
                    : copied === 'pending'
                      ? 'Not received yet'
                      : 'Copy secret'}
                </button>
              )}
            </div>
          ) : (
            <div className="text-amber-600 dark:text-amber-400 mt-0.5">
              Set DASHBOARD_PUBLIC_URL to get this subscription's webhook URL.
            </div>
          )
        )}
        {sub.last_error && (
          <div className="text-red-600 dark:text-red-400 truncate"
               title={sub.last_error}>
            {sub.last_error}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onDelete}
        disabled={isPending}
        className="text-red-600 hover:underline disabled:opacity-50"
      >
        Delete
      </button>
    </li>
  )
}

/** Whose subscription this is: yours, or an agent's. */
export function ScopePill({
  sub,
  agentLabel,
}: {
  sub: Pick<WebhookSubscription, 'scope' | 'agent'>
  agentLabel?: string
}) {
  const isAgent = sub.scope === 'service'
  return (
    <span
      className={`inline-block px-1.5 rounded-sm text-[10px] uppercase font-semibold ${
        isAgent
          ? 'bg-brand/10 text-brand'
          : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
      }`}
      title={isAgent ? `Created for agent ${sub.agent ?? ''}` : 'Your personal subscription'}
    >
      {isAgent ? `Agent ${agentLabel || sub.agent || ''}` : 'Personal'}
    </span>
  )
}

export function StatusPill({ status }: { status: WebhookSubscription['status'] }) {
  const map: Record<WebhookSubscription['status'], string> = {
    active: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
    creating: 'bg-blue-100 text-blue-800',
    failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
    renew_failed: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400',
    expired: 'bg-gray-100 text-gray-800',
    disabled: 'bg-gray-100 text-gray-800',
  }
  return (
    <span
      className={`inline-block px-1.5 rounded-sm text-[10px] uppercase font-semibold ${map[status]}`}
    >
      {status.replace('_', ' ')}
    </span>
  )
}
