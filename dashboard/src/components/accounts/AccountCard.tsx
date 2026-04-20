/**
 * One labeled account row (minimized = identity + ⭐ default toggle + Edit;
 * expanded = full credentials form + per-agent override list).
 *
 * When the MCP declares a `credentials.webhooks` block, an
 * "Active subscriptions" sub-panel appears below the identity row with
 * per-row delete + a "Subscribe to events" CTA that opens the modal.
 */

import type { AccountSummary, Integration } from '../../api/credentials'
import type { AccountsOps } from './types'
import { AccountForm } from './AccountForm'
import { SubscriptionsPanel } from './SubscriptionsPanel'

interface Props {
  integration: Integration
  account: AccountSummary
  ops: AccountsOps
  isEditing: boolean
  onEdit: () => void
  onClose: () => void
}

export function AccountCard({
  integration,
  account,
  ops,
  isEditing,
  onEdit,
  onClose,
}: Props) {
  const isOAuth = !!integration.oauth
  const label = account.display_email || account.account_label
  const overridesCount = account.agent_overrides.length

  return (
    <div className="border border-p-border-light rounded-lg bg-gray-50 dark:bg-gray-800/40">
      <div className="flex items-center justify-between px-3 py-2">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <button
            onClick={() => {
              if (account.is_default) return
              ops.setDefault.mutate({
                mcpName: integration.mcp_name,
                accountLabel: account.account_label,
              })
            }}
            disabled={account.is_default || ops.setDefault.isPending}
            title={
              account.is_default
                ? 'This is the default account'
                : 'Set as default'
            }
            className={`text-lg leading-none ${
              account.is_default
                ? 'text-amber-500'
                : 'text-gray-300 hover:text-amber-400'
            }`}
          >
            ★
          </button>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-p-text truncate">
              {label}
              {account.is_default && (
                <span className="ml-2 text-xs text-p-text-light">
                  (default)
                </span>
              )}
            </div>
            <div className="text-xs text-p-text-light flex items-center gap-2 flex-wrap">
              {isOAuth && account.connected_services.length > 0 && (
                <span>
                  {account.connected_services.length} service
                  {account.connected_services.length === 1 ? '' : 's'}
                </span>
              )}
              {overridesCount > 0 && (
                <span className="text-brand">
                  Used for {overridesCount} agent
                  {overridesCount === 1 ? '' : 's'}
                </span>
              )}
              {account.missing_scopes.length > 0 && (
                <span className="text-amber-600 dark:text-amber-400">
                  Missing access
                </span>
              )}
            </div>
          </div>
        </div>
        <button
          onClick={onEdit}
          className="text-sm text-brand hover:underline ml-2"
        >
          {isEditing ? 'Close' : 'Edit'}
        </button>
      </div>

      {/* Vendor webhook subscriptions sub-panel. The component
          self-hides for MCPs that don't declare a credentials.webhooks block. */}
      <div className="px-3 pb-2">
        <SubscriptionsPanel
          mcpName={integration.mcp_name}
          accountLabel={account.account_label}
        />
      </div>

      {isEditing && (
        <div className="border-t border-p-border-light p-3">
          <AccountForm
            integration={integration}
            account={account}
            ops={ops}
            onDone={onClose}
            onCancel={onClose}
          />
        </div>
      )}
    </div>
  )
}
