/**
 * One webhook subscription row, shared by the Connected Accounts panel and
 * the agent's MCPs tab: status + scope pills, the vendor target, the events,
 * the manual-mode webhook URL / secret, and a Delete that walks the
 * linked-triggers refusal (409 → confirm → force).
 */

import { useState } from 'react'
import { fetchSigningSecret, type WebhookSubscription } from '../../api/subscriptions'
import { isRelayDelivery } from './useDeleteWithConfirm'
import { BADGE_BASE, BADGE_TONE } from '../ui/classes'

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
  registrationMode: 'relay' | 'auto' | 'manual' | 'app'
  perSubscriptionSecret: boolean
  /** Display name of the agent a service row belongs to. */
  agentLabel?: string
  onDelete: () => void
  isPending: boolean
}) {
  const [copied, setCopied] = useState<string | null>(null)
  const events = sub.selected_events.join(', ')
  const isRelay = isRelayDelivery(sub)
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
            <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}>
              {sub.target_kind}
            </span>
          )}
          {isRelay && (
            <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}>
              {/* plain-sentences: allow: the operator keeps this pill's words as they are */}
              via OtoDock
            </span>
          )}
        </div>
        <div className="text-tertiary truncate" title={events}>
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
                className="font-mono text-3xs text-tertiary truncate"
                title={webhookUrl}
              >
                {webhookUrl}
              </span>
              <button
                type="button"
                onClick={() => copy('url', webhookUrl)}
                className="text-link hover:underline shrink-0"
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
                  className="text-link hover:underline shrink-0"
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
            <div className="text-status-warning-fg mt-0.5">
              Set DASHBOARD_PUBLIC_URL to get this subscription's webhook URL.
            </div>
          )
        )}
        {sub.last_error && (
          <div className="text-status-danger-fg truncate"
               title={sub.last_error}>
            {sub.last_error}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onDelete}
        disabled={isPending}
        className="text-status-danger-fg hover:underline disabled:opacity-50"
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
      className={`${BADGE_BASE} ${BADGE_TONE[isAgent ? 'agent' : 'neutral']}`}
      title={isAgent ? `Created for agent ${sub.agent ?? ''}` : 'Your personal subscription'}
    >
      {isAgent ? `Agent ${agentLabel || sub.agent || ''}` : 'Personal'}
    </span>
  )
}

export function StatusPill({ status }: { status: WebhookSubscription['status'] }) {
  const map: Record<WebhookSubscription['status'], keyof typeof BADGE_TONE> = {
    active: 'success',
    creating: 'info',
    failed: 'danger',
    renew_failed: 'warning',
    expired: 'neutral',
    disabled: 'neutral',
  }
  return (
    <span
      className={`${BADGE_BASE} ${BADGE_TONE[map[status]]}`}
    >
      {status.replace('_', ' ')}
    </span>
  )
}
