/**
 * Wraps the right form (OAuth vs plain creds) for one account, plus the
 * per-agent override checklist that's only shown when editing an existing
 * account with >1 connected accounts to choose between.
 */

import type { AccountSummary, Integration } from '../../api/credentials'
import type { AccountsOps } from './types'
import { OAuthAccountForm } from './OAuthAccountForm'
import { PlainCredsAccountForm } from './PlainCredsAccountForm'
import { AgentOverrideList } from './AgentOverrideList'

interface Props {
  integration: Integration
  account: AccountSummary | null
  ops: AccountsOps
  onDone: () => void
  onCancel: () => void
}

export function AccountForm({ integration, account, ops, onDone, onCancel }: Props) {
  const isOAuth = !!integration.oauth
  const isNew = account === null

  return (
    <div className="space-y-4">
      {isOAuth ? (
        <OAuthAccountForm
          integration={integration}
          account={account}
          onDone={onDone}
        />
      ) : (
        <PlainCredsAccountForm
          integration={integration}
          account={account}
          ops={ops}
          onDone={onDone}
        />
      )}

      {/* Per-agent override checklist — only when editing an existing
          account AND multi-account is supported AND there are multiple
          accounts to choose between. */}
      {!isNew && integration.accounts.length > 1 && (
        <AgentOverrideList
          integration={integration}
          account={account!}
          ops={ops}
        />
      )}

      <div className="flex justify-end gap-2 pt-2 border-t border-p-border-light">
        <button
          onClick={onCancel}
          className="px-3 py-1.5 text-sm text-p-text-secondary hover:text-p-text"
        >
          Close
        </button>
      </div>
    </div>
  )
}
