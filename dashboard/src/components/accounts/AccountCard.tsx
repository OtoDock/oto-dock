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
import { InstallationsPanel } from './InstallationsPanel'
import { SubscriptionsPanel } from './SubscriptionsPanel'
import { isGitHubApp } from '../../lib/kinds/oauthApp'
import { useSaveState } from '../../hooks/useSaveState'
import { CARD_BORDERED } from '../ui/classes'
import { SaveState } from '../ui/SettingsControls'

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
  const { stateOf, run } = useSaveState()
  const isOAuth = !!integration.oauth
  const label = account.display_email || account.account_label
  // A user-chosen label (PAT saves) is otherwise invisible when the provider
  // identity fills display_email — two same-identity accounts then render as
  // twins. Show the label in parens whenever it differs from the title.
  const customLabel =
    account.display_email &&
    account.account_label &&
    account.account_label !== account.display_email
      ? account.account_label
      : ''
  const overridesCount = account.agent_overrides.length
  const githubApp = isGitHubApp(account.kind)
  const installationsCount = account.installations?.length ?? 0

  return (
    <div className={CARD_BORDERED}>
      <div className="flex items-center justify-between px-3 py-2">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <button
            onClick={() => {
              if (account.is_default) return
              void run('default', () => ops.setDefault.mutateAsync({
                mcpName: integration.mcp_name,
                accountLabel: account.account_label,
              }), { failTitle: 'The default account was not set' })
            }}
            disabled={account.is_default || ops.setDefault.isPending}
            title={
              account.is_default
                ? 'This is the default account'
                : 'Set as default'
            }
            className={`text-lg leading-none ${
              account.is_default
                ? 'text-link'
                : 'text-tertiary hover:text-link'
            }`}
          >
            ★
          </button>
          <div className="min-w-0 flex-1">
            {/* The save state sits outside the truncated name, so a long
                address never cuts it off. */}
            <div className="flex items-center">
              <div className="min-w-0 text-sm font-medium text-primary truncate">
                {label}
                {customLabel && (
                  <span className="ml-2 text-xs text-tertiary">
                    {customLabel}
                  </span>
                )}
                {account.is_default && (
                  <span className="ml-2 text-xs text-tertiary">
                    Default
                  </span>
                )}
              </div>
              <SaveState state={stateOf('default')} />
            </div>
            <div className="text-xs text-tertiary flex items-center gap-2 flex-wrap">
              {githubApp && (
                <span>
                  {installationsCount} installation
                  {installationsCount === 1 ? '' : 's'}
                </span>
              )}
              {isOAuth && account.connected_services.length > 0 && (
                <span>
                  {account.connected_services.length} service
                  {account.connected_services.length === 1 ? '' : 's'}
                </span>
              )}
              {overridesCount > 0 && (
                <span className="text-status-info-fg">
                  Used for {overridesCount} agent
                  {overridesCount === 1 ? '' : 's'}
                </span>
              )}
              {account.missing_scopes.length > 0 && (
                <span className="text-status-warning-fg">
                  Missing access
                </span>
              )}
              {account.needs_reconnect && (
                <span className="text-status-warning-fg">
                  Reconnect needed
                </span>
              )}
            </div>
          </div>
        </div>
        <button
          onClick={onEdit}
          className="text-sm text-link hover:text-link-hover ml-2"
        >
          {isEditing ? 'Close' : 'Edit'}
        </button>
      </div>

      {/* The installations of a GitHub-App account, then the vendor webhook
          subscriptions sub-panel (self-hides for MCPs that don't declare a
          credentials.webhooks block). */}
      <div className="px-3 pb-2">
        {githubApp && (
          <InstallationsPanel integration={integration} account={account} />
        )}
        <SubscriptionsPanel
          mcpName={integration.mcp_name}
          accountLabel={account.account_label}
          serviceBindings={account.service_bindings ?? []}
        />
      </div>

      {isEditing && (
        <div className="border-t border-subtle p-3">
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
