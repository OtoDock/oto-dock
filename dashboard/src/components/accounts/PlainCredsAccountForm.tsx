/**
 * Plain credentials (username/password) form. Same logic for user-scope and
 * service-scope — scope-specific mutations come in via the ``ops`` bundle.
 */

import { useState } from 'react'
import type {
  AccountSummary,
  CredentialField,
  Integration,
} from '../../api/credentials'
import type { AccountsOps } from './types'

interface Props {
  integration: Integration
  account: AccountSummary | null
  ops: AccountsOps
  onDone: () => void
}

export function PlainCredsAccountForm({ integration, account, ops, onDone }: Props) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [accountLabel, setAccountLabel] = useState(
    account?.account_label || '',
  )
  const [error, setError] = useState('')

  const fields = integration.fields || []
  const requireLabel = !account

  const handleSave = async () => {
    setError('')
    let finalLabel = (accountLabel || '').trim()
    if (!finalLabel && account) finalLabel = account.account_label
    if (!finalLabel && integration.accounts.length === 0) finalLabel = 'default'
    if (!finalLabel) {
      finalLabel = (values['EMAIL_USER'] || '').trim() || 'account'
    }

    try {
      await ops.setIntegration.mutateAsync({
        mcpName: integration.mcp_name,
        credentials: values,
        accountLabel: finalLabel,
      })
      onDone()
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Failed to save'
      setError(message)
    }
  }

  const handleRemove = async () => {
    if (!account) return
    if (
      !confirm(
        `Remove ${account.display_email || account.account_label} from ${integration.display_name}?`,
      )
    )
      return
    try {
      await ops.deleteIntegration.mutateAsync({
        mcpName: integration.mcp_name,
        accountLabel: account.account_label,
      })
      onDone()
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Failed to remove'
      setError(message)
    }
  }

  return (
    <div className="space-y-3">
      {requireLabel && (
        <div>
          <label className="block text-sm text-p-text-secondary mb-1">
            Account label
          </label>
          <input
            type="text"
            placeholder="e.g. Work, Personal"
            value={accountLabel}
            onChange={(e) => setAccountLabel(e.target.value)}
            className="w-full px-3 py-2 border border-p-border-light rounded-lg text-sm bg-white dark:bg-gray-900 focus:outline-hidden focus:ring-1 focus:ring-brand text-p-text placeholder:text-p-text-light"
          />
          <div className="text-xs text-p-text-light mt-1">
            Choose a friendly name to tell accounts apart (defaults to the
            email/username you enter below).
          </div>
        </div>
      )}

      {fields.map((field: CredentialField) => (
        <div key={field.key}>
          <label className="block text-sm text-p-text-secondary mb-1">
            {field.label}
          </label>
          <input
            type={field.input_type === 'password' ? 'password' : 'text'}
            placeholder={
              account?.configured_keys.includes(field.key)
                ? '(configured)'
                : ''
            }
            value={values[field.key] || ''}
            onChange={(e) =>
              setValues((prev) => ({ ...prev, [field.key]: e.target.value }))
            }
            className="w-full px-3 py-2 border border-p-border-light rounded-lg text-sm bg-white dark:bg-gray-900 focus:outline-hidden focus:ring-1 focus:ring-brand text-p-text placeholder:text-p-text-light"
          />
        </div>
      ))}

      <div className="flex gap-2">
        <button
          onClick={handleSave}
          disabled={ops.setIntegration.isPending}
          className="px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-lg text-sm disabled:opacity-50"
        >
          {ops.setIntegration.isPending ? 'Saving...' : account ? 'Save' : 'Add account'}
        </button>
        {account && (
          <button
            onClick={handleRemove}
            disabled={ops.deleteIntegration.isPending}
            className="px-4 py-2 border border-p-accent-red text-p-accent-red rounded-lg text-sm hover:bg-red-50 dark:hover:bg-red-900/20"
          >
            {ops.deleteIntegration.isPending ? 'Removing...' : 'Remove'}
          </button>
        )}
      </div>

      {error && <div className="text-sm text-p-accent-red">{error}</div>}
    </div>
  )
}
