/**
 * Top-level multi-account manager card. Renders the integration title,
 * status chip, "Add another account" affordance, and the list of
 * ``AccountCard``s.
 *
 * Driven by ``UserAccountsManager`` (per-user MCPs), which supplies the
 * scope-specific mutation hooks via the ``ops`` bundle.
 */

import { useState } from 'react'
import type { Integration } from '../../api/credentials'
import type { AccountsOps } from './types'
import { AccountCard } from './AccountCard'
import { AccountForm } from './AccountForm'

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
    <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
      <button
        className="w-full flex items-center justify-between p-4 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div>
          <div className="font-medium text-p-text">
            {integration.display_name}
          </div>
          <div className="text-sm text-p-text-secondary">
            {integration.description}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {accounts.length > 0 ? (
            <span className="text-xs px-2 py-0.5 rounded-lg bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400">
              {accounts.length} account{accounts.length === 1 ? '' : 's'}
            </span>
          ) : (
            <span className="text-xs px-2 py-0.5 rounded-lg bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
              Not Connected
            </span>
          )}
          <svg
            className={`w-4 h-4 text-p-text-light transition-transform ${
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
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-p-border-light pt-3 space-y-3">
          {/* "Add new account" button */}
          {(hasMulti || accounts.length === 0) && !adding && (
            <button
              className="w-full px-3 py-2 border border-dashed border-p-border-light rounded-lg text-sm text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800"
              onClick={() => {
                setAdding(true)
                setEditingLabel(null)
              }}
            >
              + Add {accounts.length === 0 ? '' : 'another '}
              {integration.display_name} account
            </button>
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
