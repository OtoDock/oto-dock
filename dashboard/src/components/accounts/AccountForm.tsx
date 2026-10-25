/**
 * Wraps the right form (OAuth vs plain creds) for one account, plus the
 * per-agent override checklist that's shown when editing an existing
 * account with >1 connected accounts to choose between, or on GitHub,
 * where it also holds each agent's repositories and Read only.
 */

import type { AccountSummary, Integration } from '../../api/credentials'
import type { AccountsOps } from './types'
import { OAuthAccountForm } from './OAuthAccountForm'
import { PlainCredsAccountForm } from './PlainCredsAccountForm'
import { AgentOverrideList } from './AgentOverrideList'
import Button from '../ui/Button'

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
          account AND there are multiple accounts to choose between, or the
          checklist also narrows the agents' GitHub reach (one account is
          the common case there). */}
      {!isNew && (integration.accounts.length > 1 || !!integration.reach_supported) && (
        <AgentOverrideList
          integration={integration}
          account={account!}
          ops={ops}
        />
      )}

      <div className="flex justify-end gap-2 pt-2 border-t border-subtle">
        <Button variant="ghost" size="sm"
          onClick={onCancel}
        >
          Close
        </Button>
      </div>
    </div>
  )
}
