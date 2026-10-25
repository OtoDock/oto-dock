/**
 * Top-level multi-account manager card. Renders the integration title,
 * status chip, the "Add an account" or "Add another account" button, and
 * the list of ``AccountCard``s.
 *
 * Driven by ``UserAccountsManager`` (per-user MCPs), which supplies the
 * scope-specific mutation hooks via the ``ops`` bundle.
 */

import { useState } from 'react'
import type { Integration } from '../../api/credentials'
import type { AccountsOps } from './types'
import { AccountCard } from './AccountCard'
import { AccountForm } from './AccountForm'
import Button from '../ui/Button'
import { CARD } from '../ui/classes'
import { Badge } from '../ui/SettingsControls'
import InfoTip from '../ui/InfoTip'

interface Props {
  integration: Integration
  ops: AccountsOps
}

export function AccountsManager({ integration, ops }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [editingLabel, setEditingLabel] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const accounts = integration.accounts || []
  const hasMulti = integration.supports_multi_account

  return (
    <div className={CARD}>
      {/* The toggle is stretched over the whole header (its ::after), so a
          press anywhere opens the card; the info button sits above it. */}
      <div className="relative flex items-center justify-between p-4">
        <div className="min-w-0 flex-1 pr-3 font-medium text-primary">
          <button
            type="button"
            aria-expanded={expanded}
            className="text-left after:absolute after:inset-0 after:rounded-card focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-focus-ring"
            onClick={() => setExpanded(!expanded)}
          >
            {integration.display_name}
          </button>
          {integration.description && (
            <span className="relative z-10">
              <InfoTip label={integration.display_name}>{integration.description}</InfoTip>
            </span>
          )}
        </div>
        {/* A rotated chevron stacks above the stretched toggle: no pointer here. */}
        <div className="pointer-events-none flex items-center gap-2 shrink-0">
          {accounts.length > 0 ? (
            <Badge variant="success">
              {accounts.length} account{accounts.length === 1 ? '' : 's'}
            </Badge>
          ) : (
            <Badge variant="warning">
              Not Connected
            </Badge>
          )}
          <svg
            className={`w-4 h-4 text-tertiary transition-transform ${
              expanded ? 'rotate-180' : ''
            }`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M19 9l-7 7-7-7"
            />
          </svg>
        </div>
      </div>

      {expanded && (
        <div className="px-4 pb-4 border-t border-subtle pt-3 space-y-3">
          {/* The group's primary action. The integration's own name stays
              out of the label: several end in "Account" already. */}
          {(hasMulti || accounts.length === 0) && !adding && (
            <Button variant="primary" size="md"
              className="w-full"
              onClick={() => {
                setAdding(true)
                setEditingLabel(null)
              }}
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              {accounts.length === 0 ? 'Add an account' : 'Add another account'}
            </Button>
          )}

          {adding && (
            <AccountForm
              integration={integration}
              account={null}
              ops={ops}
              onDone={() => setAdding(false)}
              onCancel={() => setAdding(false)}
            />
          )}

          {/* Account list */}
          {accounts.map((acc) => (
            <AccountCard
              key={acc.account_label}
              integration={integration}
              account={acc}
              ops={ops}
              isEditing={editingLabel === acc.account_label}
              onEdit={() =>
                setEditingLabel((cur) =>
                  cur === acc.account_label ? null : acc.account_label,
                )
              }
              onClose={() => setEditingLabel(null)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
