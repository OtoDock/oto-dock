import { useState } from 'react'
import {
  useMyIntegrations,
} from '../api/credentials'
import { UserAccountsManager } from '../components/UserAccountsManager'
import {
  useUserApiKeys,
  useCreateUserApiKey,
  useRevokeUserApiKey,
  type CreatedUserApiKey,
} from '../api/userApiKeys'

function ApiKeysSection() {
  const { data: keys = [], isLoading } = useUserApiKeys()
  const createM = useCreateUserApiKey()
  const revokeM = useRevokeUserApiKey()
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [perms, setPerms] = useState<{ triggers: boolean }>({ triggers: true })
  const [createdKey, setCreatedKey] = useState<CreatedUserApiKey | null>(null)

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim()) {
      alert('Key name required')
      return
    }
    const permList: string[] = []
    if (perms.triggers) permList.push('triggers')
    if (permList.length === 0) {
      alert('At least one permission required')
      return
    }
    try {
      const created = await createM.mutateAsync({ name: newName.trim(), permissions: permList })
      setCreatedKey(created)
      setNewName('')
      setShowCreate(false)
    } catch (e: any) {
      alert(`Create failed: ${e?.message ?? e}`)
    }
  }

  const onCopy = async () => {
    if (!createdKey) return
    try { await navigator.clipboard.writeText(createdKey.key) } catch { /* ignore */ }
  }

  const onRevoke = async (id: string, name: string) => {
    if (!confirm(`Revoke key "${name}"? Webhooks using it will immediately fail.`)) return
    try {
      await revokeM.mutateAsync(id)
    } catch (e: any) {
      alert(`Revoke failed: ${e?.message ?? e}`)
    }
  }

  return (
    <div className="mb-8">
      <h2 className="text-lg font-medium text-p-text mb-3">API Keys</h2>
      <p className="text-sm text-p-text-secondary mb-4">
        Personal API keys for webhook triggers. External systems authenticate to
        your user-scoped trigger webhooks with these.
      </p>

      {isLoading ? (
        <p className="text-sm text-p-text-secondary">Loading...</p>
      ) : (
        <div className="space-y-2">
          {keys.length === 0 && (
            <p className="text-sm text-p-text-secondary">No API keys yet.</p>
          )}
          {keys.map((k) => (
            <div key={k.id} className="bg-white dark:bg-p-surface rounded-lg border border-p-border-light p-3 flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-p-text">{k.name}</span>
                  {k.permissions.map((p) => (
                    <span key={p} className="px-1.5 py-0.5 text-xs rounded-sm bg-p-bg text-p-text-secondary">{p}</span>
                  ))}
                  {k.revoked_at && (
                    <span className="px-1.5 py-0.5 text-xs rounded-sm bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400">revoked</span>
                  )}
                </div>
                <p className="text-xs text-p-text-secondary mt-0.5">
                  Created {new Date(k.created_at).toLocaleDateString()}
                  {k.last_used_at ? ` · last used ${new Date(k.last_used_at).toLocaleString()}` : ' · never used'}
                </p>
              </div>
              {!k.revoked_at && (
                <button
                  onClick={() => onRevoke(k.id, k.name)}
                  className="px-2 py-1 rounded-sm text-xs font-medium bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50"
                >
                  Revoke
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <button
        onClick={() => setShowCreate(true)}
        className="mt-3 px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover"
      >
        + Create key
      </button>

      {showCreate && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={() => setShowCreate(false)}>
          <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light max-w-md w-full p-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-p-text mb-3">Create API key</h3>
            <form onSubmit={onCreate} className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">Name</label>
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="e.g. GitHub webhook"
                  className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">Permissions</label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={perms.triggers}
                    onChange={(e) => setPerms({ ...perms, triggers: e.target.checked })}
                  />
                  <span>triggers — fire user-scoped webhook triggers</span>
                </label>
                <p className="text-xs text-p-text-secondary mt-2">
                  More permissions (chat / tasks / notifications) coming soon.
                </p>
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowCreate(false)} className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm">
                  Cancel
                </button>
                <button type="submit" disabled={createM.isPending} className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover disabled:opacity-50">
                  {createM.isPending ? 'Creating…' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {createdKey && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={() => setCreatedKey(null)}>
          <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light max-w-md w-full p-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-p-text mb-2">API key created</h3>
            <p className="text-sm text-amber-700 bg-amber-50 rounded-sm p-2 mb-3">
              <span className="font-medium">Copy this key now.</span> It will not be shown again — if you lose it, revoke and recreate.
            </p>
            <div className="bg-p-bg rounded-sm p-2 mb-3 font-mono text-xs break-all">{createdKey.key}</div>
            <div className="flex justify-end gap-2">
              <button onClick={onCopy} className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover">
                Copy to clipboard
              </button>
              <button onClick={() => setCreatedKey(null)} className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm">
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Integrations Tab (connected MCP accounts + personal trigger API keys)
// ---------------------------------------------------------------------------

export function IntegrationsTab() {
  const { data: integrations, isLoading } = useMyIntegrations()
  return (
    <div>
      <div className="mb-8">
        <h2 className="text-lg font-medium text-p-text mb-3">Connected Accounts</h2>
        <p className="text-sm text-p-text-secondary mb-4">
          Configure your personal credentials for MCP tools. These are used when agents access external services on your behalf.
        </p>
        {isLoading ? (
          <div className="text-sm text-p-text-light">Loading...</div>
        ) : integrations && integrations.length > 0 ? (
          <div className="space-y-3">
            {integrations.map(i => (
              <UserAccountsManager key={i.mcp_name} integration={i} />
            ))}
          </div>
        ) : (
          <div className="text-sm text-p-text-light border border-p-border-light rounded-xl p-6 text-center">
            No integrations available for your assigned agents.
          </div>
        )}
      </div>
      <ApiKeysSection />
    </div>
  )
}
