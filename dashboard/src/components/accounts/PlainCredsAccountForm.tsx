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
import { useDialogs } from '../ui/dialogs'
import Button from '../ui/Button'
import { Input } from '../ui/Field'

interface Props {
  integration: Integration
  account: AccountSummary | null
  ops: AccountsOps
  onDone: () => void
}

export function PlainCredsAccountForm({ integration, account, ops, onDone }: Props) {
  const dialogs = useDialogs()
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
    if (!(await dialogs.confirm({
      title: `Remove ${account.display_email || account.account_label} from ${integration.display_name}?`,
      confirmLabel: 'Remove',
      tone: 'danger',
    })))
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
          <label className="block text-sm text-secondary mb-1">
            Account label
          </label>
          <Input
            type="text"
            placeholder="e.g. Work, Personal"
            value={accountLabel}
            onChange={(e) => setAccountLabel(e.target.value)}
          />
          <div className="text-xs text-tertiary mt-1">
            Choose a friendly name to tell accounts apart. Left empty, it is
            the email or username you enter below.
          </div>
        </div>
      )}

      {account?.missing_required?.length ? (
        <div className="text-xs text-status-warning-fg">
          This account needs {account.missing_required.join(', ')} before
          its agents can use it.
        </div>
      ) : null}

      {fields.map((field: CredentialField) => (
        <div key={field.key}>
          <label className="block text-sm text-secondary mb-1">
            {field.label}
            {field.required === false && (
              <span className="text-tertiary">, optional</span>
            )}
          </label>
          <Input
            type={field.input_type === 'password' ? 'password' : 'text'}
            placeholder={
              account?.configured_keys.includes(field.key)
                ? 'Configured'
                : ''
            }
            value={values[field.key] || ''}
            onChange={(e) =>
              setValues((prev) => ({ ...prev, [field.key]: e.target.value }))
            }
          />
        </div>
      ))}

      <div className="flex gap-2">
        <Button variant="primary" size="md"
          onClick={handleSave}
          disabled={ops.setIntegration.isPending}
        >
          {ops.setIntegration.isPending ? 'Saving...' : account ? 'Save' : 'Add account'}
        </Button>
        {account && (
          <Button variant="danger" size="md"
            onClick={handleRemove}
            disabled={ops.deleteIntegration.isPending}
          >
            {ops.deleteIntegration.isPending ? 'Removing...' : 'Remove'}
          </Button>
        )}
      </div>

      {error && <div className="text-sm text-status-danger-fg">{error}</div>}
    </div>
  )
}
