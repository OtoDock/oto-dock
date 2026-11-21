/**
 * The agent's own vendor subscriptions of one of its accounts, on Agent
 * Settings → MCPs under that account: a "Subscribe to events" button (the
 * SubscribeToEventsModal with the agent fixed and this account) and the
 * account's service-scope rows with delete. Each account of the agent keeps
 * its own; taking an account out removes them. This is the surface a manager
 * reaches when an agent trigger or a shared app needs the events; a
 * co-manager may subscribe here with another manager's account.
 *
 * `AgentLeftoverSubscriptions` lists the agent's service rows on no account
 * it holds (kept from an older version), each with delete.
 *
 * Self-hides for MCPs without a `credentials.webhooks` block (the catalog
 * endpoint answers 404).
 */

import { useState } from 'react'
import {
  useSubscriptions,
  useWebhookEventCatalog,
} from '../../api/subscriptions'
import { INSTALLATION_TARGET_KIND } from '../../lib/kinds/oauthApp'
import { SubscribeToEventsModal } from './SubscribeToEventsModal'
import { SubscriptionRow } from './SubscriptionRow'
import { useDeleteWithConfirm } from './useDeleteWithConfirm'

interface Props {
  agentName: string
  mcpName: string
  /** The agent's account these subscriptions register with. */
  accountLabel: string
  /** Its owner no longer manages the agent: listed, no new subscription. */
  paused?: boolean
  /** The agent's default account. A run a trigger starts takes the default,
   * so under another account its subscriptions say so. */
  isDefault?: boolean
}

export function AgentServiceSubscriptions({
  agentName, mcpName, accountLabel, paused = false, isDefault = true,
}: Props) {
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

  const rows = (subscriptionsQuery.data ?? []).filter((r) => r.account_label === accountLabel)
  // An account whose owner left answers no catalog: its rows still show.
  if (catalog.isLoading || (!catalog.data && rows.length === 0)) return null
  const registrationMode = catalog.data?.registration?.mode ?? 'manual'

  return (
    <div className="mt-1 pl-1 border-l-2 border-subtle" data-testid="agent-service-subscriptions">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-3xs uppercase tracking-wide text-tertiary">Event subscriptions</span>
        {!paused && catalog.data && (
          <button
            type="button"
            onClick={() => {
              del.setError(null)
              setShowModal(true)
            }}
            className="text-xs text-link hover:underline"
          >
            Subscribe to events
          </button>
        )}
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-tertiary italic mt-1">None yet.</div>
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
      {!isDefault && rows.length > 0 && (
        <div className="text-xs text-tertiary mt-1">Triggers from these run on the default account.</div>
      )}
      {del.error && (
        <div className="mt-1 text-xs text-status-danger-fg">{del.error}</div>
      )}
      {showModal && catalog.data && (
        <SubscribeToEventsModal
          mcpName={mcpName}
          accountLabel={accountLabel}
          providerId={catalog.data.provider_id}
          eventCatalog={catalog.data.event_catalog}
          vendorTargetSpec={catalog.data.vendor_target_spec}
          registrationMode={registrationMode}
          manualInstructionsUrl={catalog.data.registration?.manual_instructions_url}
          vendorTargetPrefill={catalog.data.vendor_target_prefill}
          installationTarget={catalog.data.target_kind === INSTALLATION_TARGET_KIND}
          grantedScopes={catalog.data.granted_scopes}
          scopeServices={catalog.data.scope_services}
          scope="service"
          agent={agentName}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}

interface LeftoverProps {
  agentName: string
  mcpName: string
  /** The labels of the accounts the agent holds. */
  heldLabels: string[]
}

/** The agent's service subscriptions on no account it holds, each removable.
 * Renders nothing when there are none. */
export function AgentLeftoverSubscriptions({ agentName, mcpName, heldLabels }: LeftoverProps) {
  const subscriptionsQuery = useSubscriptions({
    mcp_name: mcpName,
    scope: 'service',
    agent: agentName,
  })
  const del = useDeleteWithConfirm()
  const held = new Set(heldLabels)
  const rows = (subscriptionsQuery.data ?? []).filter((r) => !held.has(r.account_label))
  if (rows.length === 0) return null

  return (
    <div className="mt-2 pl-1 border-l-2 border-subtle" data-testid="agent-leftover-subscriptions">
      <span className="text-3xs uppercase tracking-wide text-tertiary">
        On an account this agent no longer has
      </span>
      <ul className="space-y-1 mt-1">
        {rows.map((r) => (
          <SubscriptionRow
            key={r.id}
            sub={r}
            webhookBase=""
            registrationMode="auto"
            perSubscriptionSecret={false}
            agentLabel={agentName}
            onDelete={() => void del.remove(r)}
            isPending={del.isPending}
          />
        ))}
      </ul>
      {del.error && (
        <div className="mt-1 text-xs text-status-danger-fg">{del.error}</div>
      )}
    </div>
  )
}
