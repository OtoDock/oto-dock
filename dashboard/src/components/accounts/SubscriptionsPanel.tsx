/**
 * "Active subscriptions" sub-panel inside an AccountCard.
 *
 * Detects webhook availability by attempting to fetch the MCP's webhook
 * event catalog. The catalog endpoint returns 404 when the MCP doesn't
 * declare a `credentials.webhooks` block, so this whole panel disappears
 * for MCPs that don't support inbound vendor webhooks.
 *
 * When webhooks ARE available, shows:
 *   - one row per active subscription for this (mcp, account) pair —
 *     manual-mode vendor rows surface the webhook URL (+ signing secret
 *     when per-subscription) to paste into the vendor console; relay rows
 *     show a "via OtoDock" pill instead (hosted delivery, zero console steps)
 *   - a "+ Subscribe to events" button that opens SubscribeToEventsModal
 */

import { useState } from 'react'
import {
  fetchSigningSecret,
  useDeleteSubscription,
  useSubscriptions,
  useWebhookEventCatalog,
  type WebhookSubscription,
} from '../../api/subscriptions'
import { SubscribeToEventsModal } from './SubscribeToEventsModal'

interface Props {
  mcpName: string
  accountLabel: string
}

export function SubscriptionsPanel({
  mcpName,
  accountLabel,
}: Props) {
  const catalog = useWebhookEventCatalog(mcpName, {
    accountLabel,
    scope: 'user',
  })
  const subscriptionsQuery = useSubscriptions({
    mcp_name: mcpName,
    scope: 'user',
  })
  const del = useDeleteSubscription()
  const [showModal, setShowModal] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Hide the panel completely for MCPs that don't declare webhooks
  // (catalog endpoint returns 404 → query is in error state with no data).
  if (catalog.isLoading) return null
  if (catalog.isError) return null

  const rows = (subscriptionsQuery.data ?? []).filter(
    (r) => r.account_label === accountLabel,
  )
  const registrationMode = catalog.data?.registration?.mode ?? 'manual'

  return (
    <div className="mt-2 border-t border-p-border-light pt-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-semibold text-p-text-light">
          Active subscriptions
        </span>
        <button
          onClick={() => {
            setError(null)
            setShowModal(true)
          }}
          className="text-xs text-brand hover:underline"
        >
          + Subscribe to events
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-p-text-light italic">
          No subscriptions yet.
        </div>
      ) : (
        <ul className="space-y-1">
          {rows.map((r) => (
            <SubscriptionRow
              key={r.id}
              sub={r}
              webhookBase={catalog.data?.webhook_base ?? ''}
              registrationMode={registrationMode}
              perSubscriptionSecret={
                catalog.data?.per_subscription_secret ?? false
              }
              onDelete={() => {
                const consequence =
                  r.delivery_mode === 'relay'
                    ? 'OtoDock stops forwarding its events to this install.'
                    : 'This unregisters the webhook at the vendor.'
                if (
                  !confirm(
                    `Delete subscription for ${r.vendor_target}? ${consequence}`,
                  )
                ) {
                  return
                }
                del.mutate(r.id, {
                  onError: (e) => setError((e as Error).message),
                })
              }}
              isPending={del.isPending}
            />
          ))}
        </ul>
      )}
      {error && (
        <div className="mt-2 text-xs text-red-600 dark:text-red-400">
          {error}
        </div>
      )}
      {showModal && catalog.data && (
        <SubscribeToEventsModal
          mcpName={mcpName}
          accountLabel={accountLabel}
          providerId={catalog.data.provider_id}
          eventCatalog={catalog.data.event_catalog}
          vendorTargetSpec={catalog.data.vendor_target_spec}
          registrationMode={registrationMode}
          manualInstructionsUrl={
            catalog.data.registration?.manual_instructions_url
          }
          vendorTargetPrefill={catalog.data.vendor_target_prefill}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}

function SubscriptionRow({
  sub,
  webhookBase,
  registrationMode,
  perSubscriptionSecret,
  onDelete,
  isPending,
}: {
  sub: WebhookSubscription
  webhookBase: string
  registrationMode: 'relay' | 'auto' | 'manual'
  perSubscriptionSecret: boolean
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
    <li className="flex items-center justify-between gap-2 text-xs">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <StatusPill status={sub.status} />
          <span className="font-mono truncate">{sub.vendor_target}</span>
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
        onClick={onDelete}
        disabled={isPending}
        className="text-red-600 hover:underline disabled:opacity-50"
      >
        Delete
      </button>
    </li>
  )
}

function StatusPill({ status }: { status: WebhookSubscription['status'] }) {
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
