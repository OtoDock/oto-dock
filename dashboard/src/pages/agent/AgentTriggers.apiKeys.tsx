import { useState } from 'react'
import {
  useAgentApiKeys,
  useCreateAgentApiKey,
  useRevokeAgentApiKey,
  type CreatedAgentApiKey,
} from '../../api/agentApiKeys'
import { Modal, Field } from './AgentTriggers.modals'


// =====================================================================
// Agent API Keys section (managers only)
// =====================================================================

export function AgentApiKeysSection({ agent }: { agent: string }) {
  const { data: keys = [], isLoading } = useAgentApiKeys(agent)
  const createM = useCreateAgentApiKey()
  const revokeM = useRevokeAgentApiKey()
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [createdKey, setCreatedKey] = useState<CreatedAgentApiKey | null>(null)

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim()) {
      alert('Key name required')
      return
    }
    try {
      const created = await createM.mutateAsync({ agent, name: newName.trim(), permissions: ['triggers'] })
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
      await revokeM.mutateAsync({ agent, keyId: id })
    } catch (e: any) {
      alert(`Revoke failed: ${e?.message ?? e}`)
    }
  }

  return (
    <div className="space-y-3 border-t border-p-border-light pt-6 mt-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-p-text">Agent API Keys</h2>
          <p className="text-xs text-p-text-secondary mt-1">
            Keys that authenticate webhook fires for agent-scoped triggers.
            External systems pass them as <code>Authorization: Bearer otok_…</code>.
            The master <code>PROXY_API_KEY</code> does not work on webhook endpoints.
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="self-start sm:self-auto shrink-0 px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover"
        >
          + Create key
        </button>
      </div>

      {isLoading ? (
        <p className="text-sm text-p-text-secondary">Loading...</p>
      ) : keys.length === 0 ? (
        <p className="text-sm text-p-text-secondary">No API keys for this agent yet.</p>
      ) : (
        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="bg-white dark:bg-p-surface rounded-lg border border-p-border-light p-3 flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-p-text">{k.name}</span>
                  <span className="text-xs font-mono text-p-text-secondary">otok_{k.prefix}…</span>
                  {k.permissions.map((p) => (
                    <span key={p} className="px-1.5 py-0.5 text-xs rounded-sm bg-p-bg text-p-text-secondary">{p}</span>
                  ))}
                  {k.revoked_at && <span className="px-1.5 py-0.5 text-xs rounded-sm bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400">revoked</span>}
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

      {showCreate && (
        <Modal onClose={() => setShowCreate(false)} title={`Create API key for ${agent}`}>
          <form onSubmit={onCreate} className="space-y-3">
            <Field label="Name">
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="e.g. Uptime monitor"
                className="w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
              />
            </Field>
            <Field label="Permissions">
              <p className="text-sm text-p-text-secondary">
                <code>triggers</code> — fire agent-scoped webhook triggers
              </p>
            </Field>
            <div className="flex justify-end gap-2 pt-2">
              <button type="button" onClick={() => setShowCreate(false)} className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm">
                Cancel
              </button>
              <button type="submit" disabled={createM.isPending} className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover disabled:opacity-50">
                {createM.isPending ? 'Creating…' : 'Create'}
              </button>
            </div>
          </form>
        </Modal>
      )}

      {createdKey && (
        <Modal onClose={() => setCreatedKey(null)} title="API key created">
          <p className="text-sm text-amber-700 bg-amber-50 rounded-sm p-2 mb-3">
            <span className="font-medium">Copy this key now.</span> It won't be shown again.
          </p>
          <div className="bg-p-bg rounded-sm p-2 mb-3 font-mono text-xs break-all">{createdKey.key}</div>
          <div className="flex justify-end gap-2">
            <button onClick={onCopy} className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover">
              Copy
            </button>
            <button onClick={() => setCreatedKey(null)} className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm">
              Done
            </button>
          </div>
        </Modal>
      )}
    </div>
  )
}
