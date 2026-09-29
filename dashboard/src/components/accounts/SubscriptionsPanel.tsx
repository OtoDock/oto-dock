/**
 * "Active subscriptions" sub-panel inside an AccountCard.
 *
 * Detects webhook availability by attempting to fetch the MCP's webhook
 * event catalog. The catalog endpoint returns 404 when the MCP doesn't
 * declare a `credentials.webhooks` block, so this whole panel disappears
 * for MCPs that don't support inbound vendor webhooks.
 *
 * When webhooks ARE available, shows:
 *   - one row per subscription on this (mcp, account) pair: the user's own
 *     personal rows plus the rows of every agent this account serves as
 *     service identity, each with a scope pill; manual-mode vendor rows
 *     surface the webhook URL (+ signing secret when per-subscription) to
 *     paste into the vendor console; relay rows show a "via OtoDock" pill
 *     instead (hosted delivery, zero console steps)
 *   - a "+ Subscribe to events" button that opens SubscribeToEventsModal,
 *     with "Subscribe as" when the account serves an agent
 */

import { useState } from 'react'
import type { ServiceBindingSummary } from '../../api/credentials'
import {
  useSubscriptions,
  useWebhookEventCatalog,
} from '../../api/subscriptions'
import { useAuth } from '../../contexts/AuthContext'
import { SubscribeToEventsModal } from './SubscribeToEventsModal'
import { SubscriptionRow, useDeleteWithConfirm } from './SubscriptionRow'

interface Props {
  mcpName: string
  accountLabel: string
  /** Agents whose service identity is this account (from the account
   * summary); their service-scope rows list here too. */
  serviceBindings?: ServiceBindingSummary[]
}

export function SubscriptionsPanel({
  mcpName,
  accountLabel,
  serviceBindings = [],
}: Props) {
  const { user } = useAuth()
  const catalog = useWebhookEventCatalog(mcpName, {
    accountLabel,
    scope: 'user',
  })
  // Both scopes in one read: the list route already limits the rows to the
  // caller's own personal ones plus the agents they can access.
  const subscriptionsQuery = useSubscriptions({ mcp_name: mcpName })
  const del = useDeleteWithConfirm()
  const [showModal, setShowModal] = useState(false)

  // Hide the panel completely for MCPs that don't declare webhooks
  // (catalog endpoint returns 404 → query is in error state with no data).
  if (catalog.isLoading) return null
  if (catalog.isError) return null

  const bound = new Map(serviceBindings.map((b) => [b.agent_name, b]))
  const rows = (subscriptionsQuery.data ?? []).filter(
    (r) =>
      r.account_label === accountLabel &&
      (r.scope === 'user'
        ? r.owner === user?.sub
        : !!r.agent && bound.has(r.agent)),
  )
  const registrationMode = catalog.data?.registration?.mode ?? 'manual'

  return (
    <div className="mt-2 border-t border-p-border-light pt-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-semibold text-p-text-light">
          Active subscriptions
        </span>
        <button
          type="button"
          onClick={() => {
            del.setError(null)
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
              agentLabel={r.agent ? bound.get(r.agent)?.display_name : undefined}
              onDelete={() => void del.remove(r)}
              isPending={del.isPending}
            />
          ))}
        </ul>
      )}
      {del.error && (
        <div className="mt-2 text-xs text-red-600 dark:text-red-400">
          {del.error}
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
          serviceOptions={serviceBindings}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
