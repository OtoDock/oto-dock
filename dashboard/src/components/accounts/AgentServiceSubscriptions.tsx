/**
 * The agent's own vendor subscriptions, on Agent Settings → MCPs beneath the
 * service-account binding: a "Subscribe to events for this agent" button
 * (the SubscribeToEventsModal with the agent fixed and the bound account)
 * and the agent's service-scope rows with delete. This is the surface a
 * manager reaches when an agent trigger or a shared app needs the events;
 * a co-manager may subscribe here with the bound owner's account.
 *
 * Self-hides for MCPs without a `credentials.webhooks` block (the catalog
 * endpoint answers 404).
 */

import { useState } from 'react'
import {
  useSubscriptions,
  useWebhookEventCatalog,
} from '../../api/subscriptions'
import { SubscribeToEventsModal } from './SubscribeToEventsModal'
import { SubscriptionRow, useDeleteWithConfirm } from './SubscriptionRow'

interface Props {
  agentName: string
  mcpName: string
  /** The bound account's label: the only account a service-scope
   * subscription of this agent may use. */
  accountLabel: string
}

export function AgentServiceSubscriptions({ agentName, mcpName, accountLabel }: Props) {
  const catalog = useWebhookEventCatalog(mcpName, {
    accountLabel,
    scope: 'service',
    agent: agentName,
  })
  const subscriptionsQuery = useSubscriptions({
    mcp_name: mcpName,
    scope: 'service',
    agent: agentName,
  })
  const del = useDeleteWithConfirm()
  const [showModal, setShowModal] = useState(false)

  if (catalog.isLoading || catalog.isError || !catalog.data) return null

  const rows = subscriptionsQuery.data ?? []
  const registrationMode = catalog.data.registration?.mode ?? 'manual'

  return (
    <div className="mt-2 pl-1 border-l-2 border-p-border-light" data-testid="agent-service-subscriptions">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-wide text-p-text-light">
          Event subscriptions of this agent
        </span>
        <button
          type="button"
          onClick={() => {
            del.setError(null)
            setShowModal(true)
          }}
          className="text-xs text-brand hover:underline"
        >
          + Subscribe to events for this agent
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-p-text-light italic mt-1">
          None yet. An agent trigger, or a shared app woken by vendor events,
          needs one of these.
        </div>
      ) : (
        <ul className="space-y-1 mt-1">
          {rows.map((r) => (
            <SubscriptionRow
              key={r.id}
              sub={r}
              webhookBase={catalog.data?.webhook_base ?? ''}
              registrationMode={registrationMode}
              perSubscriptionSecret={catalog.data?.per_subscription_secret ?? false}
              agentLabel={agentName}
              onDelete={() => void del.remove(r)}
              isPending={del.isPending}
            />
          ))}
        </ul>
      )}
      {del.error && (
        <div className="mt-1 text-xs text-red-600 dark:text-red-400">{del.error}</div>
      )}
      {showModal && (
        <SubscribeToEventsModal
          mcpName={mcpName}
          accountLabel={accountLabel}
          providerId={catalog.data.provider_id}
          eventCatalog={catalog.data.event_catalog}
          vendorTargetSpec={catalog.data.vendor_target_spec}
          registrationMode={registrationMode}
          manualInstructionsUrl={catalog.data.registration?.manual_instructions_url}
          vendorTargetPrefill={catalog.data.vendor_target_prefill}
          scope="service"
          agent={agentName}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
